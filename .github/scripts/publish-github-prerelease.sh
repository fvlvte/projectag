#!/usr/bin/env bash
# Login-only GitHub prerelease backup for the Windows standalone zip.
# Cloudflare R2 is the anonymous download; this must not block that upload.
#
# GitHub's create-release API (POST /repos/.../releases) sometimes returns
# HTTP 5xx. Retry those. A missing or existing tag is not retried as 5xx.
#
#   .github/scripts/publish-github-prerelease.sh
#
# Env: GH_TOKEN or GITHUB_TOKEN, GITHUB_SHA, STANDALONE_TAG,
#      STANDALONE_SHORT, STANDALONE_ZIP, GITHUB_SERVER_URL, GITHUB_REPOSITORY,
#      GITHUB_RUN_ID. Optional: GH_RELEASE_ATTEMPTS (default 4),
#      GH_RELEASE_DELAY_SECS (default 5), GH_RELEASE_KEEP (per-commit
#      prereleases kept, default 20), GH_RELEASE_PRUNE_MAX (deletions per run,
#      default 50).
set -euo pipefail

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[[ -n "${GH_TOKEN:-}${GITHUB_TOKEN:-}" ]] || die "GH_TOKEN or GITHUB_TOKEN is required"
[[ -n "${GITHUB_SHA:-}" ]] || die "GITHUB_SHA is required"
TAG="${STANDALONE_TAG:-}"
SHORT="${STANDALONE_SHORT:-}"
ZIP="${STANDALONE_ZIP:-}"
[[ -n "$TAG" ]] || die "STANDALONE_TAG is required"
[[ -n "$SHORT" ]] || die "STANDALONE_SHORT is required"
[[ -n "$ZIP" && -f "$ZIP" ]] || die "missing zip: ${ZIP:-'(unset)'}"
ROLLING_ZIP="${STANDALONE_ROLLING_ZIP:-dist/engine-windows-standalone.zip}"
[[ -f "$ROLLING_ZIP" ]] || die "missing rolling zip: $ROLLING_ZIP"

CRYPTO="$(cd "$(dirname "$0")" && pwd)/standalone_zip_crypto.py"
python3 "$CRYPTO" verify "$ZIP"
python3 "$CRYPTO" verify "$ROLLING_ZIP"

MAX_ATTEMPTS="${GH_RELEASE_ATTEMPTS:-4}"
DELAY_SECS="${GH_RELEASE_DELAY_SECS:-5}"
[[ "$MAX_ATTEMPTS" -ge 1 ]] || die "GH_RELEASE_ATTEMPTS must be >= 1"
[[ "$DELAY_SECS" -ge 0 ]] || die "GH_RELEASE_DELAY_SECS must be >= 0"
KEEP="${GH_RELEASE_KEEP:-20}"
PRUNE_MAX="${GH_RELEASE_PRUNE_MAX:-50}"
[[ "$KEEP" =~ ^[0-9]+$ && "$KEEP" -ge 1 ]] || die "GH_RELEASE_KEEP must be a positive integer"
[[ "$PRUNE_MAX" =~ ^[0-9]+$ ]] || die "GH_RELEASE_PRUNE_MAX must be a non-negative integer"

is_retryable() {
  local log="$1"
  grep -Eqi 'HTTP 5[0-9]{2}|502 Bad Gateway|503 Service|504 Gateway|[Ss]erver [Ee]rror' "$log"
}

run_gh() {
  local attempt=1 delay="$DELAY_SECS" status=0 log
  log="$(mktemp)"
  while true; do
    if gh "$@" >"$log" 2>&1; then
      cat "$log"
      rm -f "$log"
      return 0
    fi
    status=$?
    cat "$log" >&2
    if [[ "$attempt" -ge "$MAX_ATTEMPTS" ]] || ! is_retryable "$log"; then
      rm -f "$log"
      return "$status"
    fi
    printf 'GitHub Releases retry %s/%s in %ss (not Cloudflare R2)\n' \
      "$attempt" "$MAX_ATTEMPTS" "$delay" >&2
    sleep "$delay"
    attempt="$((attempt + 1))"
    delay="$((delay * 2))"
  done
}

notes_commit() {
  cat <<EOF
API-off Windows local-sim sandbox for \`${GITHUB_SHA}\`.

Extract with 7-Zip and the separately shared QA password, then double-click \`Play.cmd\`. This is not the production patcher zip from \`scripts/deploy.sh\`.

Anonymous download is Cloudflare R2, not this GitHub backup.
Workflow: ${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}
EOF
}

notes_rolling() {
  cat <<EOF
Rolling API-off Windows local-sim sandbox from \`${GITHUB_SHA}\`.

Per-commit tag: ${TAG}
Anonymous download is Cloudflare R2, not this GitHub backup.
Workflow: ${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}
EOF
}

echo "GitHub Releases backup for ${TAG} (login-only; Cloudflare R2 is the public copy)"

if gh release view "$TAG" >/dev/null 2>&1; then
  echo "per-commit release ${TAG} already exists — uploading zip"
  run_gh release upload "$TAG" "$ZIP" --clobber
else
  run_gh release create "$TAG" \
    --prerelease \
    --title "Windows standalone ${SHORT}" \
    --notes "$(notes_commit)" \
    --target "$GITHUB_SHA" \
    "$ZIP"
fi

gh release delete standalone-windows --yes --cleanup-tag || true
run_gh release create standalone-windows \
  --prerelease \
  --title "Windows standalone (latest)" \
  --notes "$(notes_rolling)" \
  --target "$GITHUB_SHA" \
  "$ROLLING_ZIP" \
  "$ZIP"

# Bounded retention: every push adds a ~180 MB per-commit prerelease, so keep
# only the GH_RELEASE_KEEP newest `standalone-windows-<sha12>` ones (never the
# rolling `standalone-windows` or this run's tag), deleting at most
# GH_RELEASE_PRUNE_MAX per run. Best effort like the rest of this backup: a
# listing or delete failure is reported and never fails the job.
prune_prereleases() {
  local listed tag
  if ! listed="$(gh release list --limit 200 --json tagName,isPrerelease,createdAt \
      --jq '.[] | select(.isPrerelease) | select(.tagName | test("^standalone-windows-[0-9a-f]{12}$")) | [.createdAt, .tagName] | @tsv' 2>/dev/null)"; then
    echo "GitHub prerelease retention skipped: release list failed" >&2
    return 0
  fi
  printf '%s\n' "$listed" | sort -r | awk -v keep="$KEEP" \
    'NF == 2 && $2 ~ /^standalone-windows-[0-9a-f]+$/ && length($2) == 31 && ++n > keep { print $2 }' \
    | head -n "$PRUNE_MAX" | while read -r tag; do
      [[ -n "$tag" && "$tag" != "$TAG" ]] || continue
      echo "deleting superseded prerelease ${tag} (keeping the ${KEEP} newest)"
      gh release delete "$tag" --yes --cleanup-tag || echo "could not delete prerelease ${tag}" >&2
    done || true
}
prune_prereleases
