#!/usr/bin/env bash
# Tell Paperclip about the Windows standalone zip. GitHub Actions calls this
# after packing so Harmony does not copy the zip onto Hostinger.
#
# Paperclip issue attachments are capped at 10 MB. A Bevy client zip is
# larger, so this posts PAPERCLIP_DOWNLOAD_URL (Cloudflare R2 when published,
# else the private GitHub Releases URL) instead of failing the job.
#
#   .github/scripts/upload-paperclip-artifact.sh FILE.zip
#   .github/scripts/upload-paperclip-artifact.sh --print-issue-ref
#   .github/scripts/upload-paperclip-artifact.sh --dry-run FILE.zip
#   .github/scripts/upload-paperclip-artifact.sh --notify-failure
#
# Env: PAPERCLIP_API_KEY (required), PAPERCLIP_API_URL, PAPERCLIP_COMPANY_ID,
# PAPERCLIP_ISSUE_ID or PAPERCLIP_ISSUE_IDENTIFIER, PAPERCLIP_FALLBACK_ISSUE_ID,
# PAPERCLIP_DOWNLOAD_URL, PAPERCLIP_MAX_ATTACHMENT_BYTES, PAPERCLIP_RUN_ID,
# GITHUB_REF_NAME / GITHUB_HEAD_REF / GITHUB_SHA.
set -euo pipefail
REDACT_PY="$(cd "$(dirname "$0")" && pwd)/paperclip-redact.py"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
log() { printf '%s\n' "$*"; }

PRINT_REF=0
DRY_RUN=0
FILE=""
NO_COMMENT=0
NOTIFY_FAILURE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --print-issue-ref) PRINT_REF=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --no-comment) NO_COMMENT=1 ;;
    --notify-failure) NOTIFY_FAILURE=1 ;;
    -h|--help)
      sed -n '2,18p' "$0"
      exit 0
      ;;
    --*) die "unknown arg: $1" ;;
    *)
      [[ -z "$FILE" ]] || die "unexpected arg: $1"
      FILE="$1"
      ;;
  esac
  shift
done

issue_ref_from_git_ref() {
  local ref="${1:-}"
  ref="${ref#refs/heads/}"
  ref="${ref#refs/remotes/origin/}"
  local ident=""
  if [[ "$ref" =~ ([A-Za-z]+-[0-9]+) ]]; then
    ident="$(printf '%s' "${BASH_REMATCH[1]}" | tr '[:lower:]' '[:upper:]')"
  fi
  [[ -n "$ident" ]] || return 1
  printf '%s\n' "$ident"
}

git_ref="${GITHUB_HEAD_REF:-${GITHUB_REF_NAME:-${GITHUB_REF:-}}}"
ident="${PAPERCLIP_ISSUE_IDENTIFIER:-}"
if [[ -z "$ident" ]]; then
  ident="$(issue_ref_from_git_ref "$git_ref" || true)"
fi

if [[ "$PRINT_REF" -eq 1 ]]; then
  [[ -n "$ident" ]] || die "no Paperclip issue identifier in ref '${git_ref}'"
  printf '%s\n' "$ident"
  exit 0
fi

API="${PAPERCLIP_API_URL:-https://slategray-dolphin-616144.hostingersite.com}"
API="${API%/}"
COMPANY="${PAPERCLIP_COMPANY_ID:-10160982-50ee-4fc6-918a-1c85a89f58ea}"
KEY="${PAPERCLIP_API_KEY:-}"
[[ -n "$KEY" ]] || die "PAPERCLIP_API_KEY is unset — add it as a GitHub Actions secret (Paperclip board API key)"

issue_id="${PAPERCLIP_ISSUE_ID:-}"
if [[ -z "$issue_id" && -n "$ident" ]]; then
  issue_id="$ident"
fi
if [[ -z "$issue_id" ]]; then
  issue_id="${PAPERCLIP_FALLBACK_ISSUE_ID:-}"
fi
if [[ -z "$issue_id" ]]; then
  # master / non-issue branches have nothing to comment on. QA summary
  # already exits 0 here; do not fail the zip job after a successful pack.
  log "no Paperclip issue for ref '${git_ref}'; nothing posted"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf 'mode=skipped\n'
    printf 'issue=\n'
    printf 'ref=%s\n' "$git_ref"
  fi
  exit 0
fi

sha="${GITHUB_SHA:-}"
run_url="${GITHUB_SERVER_URL:-}/${GITHUB_REPOSITORY:-}/actions/runs/${GITHUB_RUN_ID:-}"
max_bytes="${PAPERCLIP_MAX_ATTACHMENT_BYTES:-10485760}"

github_zip_url() {
  if [[ -n "${PAPERCLIP_DOWNLOAD_URL:-}" ]]; then
    printf '%s\n' "$PAPERCLIP_DOWNLOAD_URL"
    return
  fi
  local repo="${GITHUB_REPOSITORY:-fvlvte/projectag}"
  local server="${GITHUB_SERVER_URL:-https://github.com}"
  local short=""
  if [[ ${#sha} -ge 12 ]]; then
    short="$(printf '%s' "$sha" | cut -c1-12)"
  fi
  [[ -n "$short" ]] || return 0
  printf '%s/%s/releases/download/standalone-windows-%s/engine-windows-standalone-%s.zip\n' \
    "$server" "$repo" "$short" "$short"
}

resolve_issue_uuid() {
  local tmp_file="$1"
  local status
  status="$(curl -sS -o "$tmp_file" -w '%{http_code}' "${auth_args[@]}" \
    "${API}/api/issues/${issue_id}")"
  if [[ "$status" -lt 200 || "$status" -ge 300 ]]; then
    die "lookup issue ${issue_id} failed HTTP ${status}"
  fi
  python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' < "$tmp_file"
}

if [[ "$NOTIFY_FAILURE" -eq 1 ]]; then
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf 'api=%s\n' "$API"
    printf 'company=%s\n' "$COMPANY"
    printf 'issue=%s\n' "$issue_id"
    printf 'mode=failure\n'
    exit 0
  fi
  auth_args=(-H "Authorization: Bearer ${KEY}" -H "Accept: application/json")
  if [[ -n "${PAPERCLIP_RUN_ID:-}" ]]; then
    auth_args+=(-H "X-Paperclip-Run-Id: ${PAPERCLIP_RUN_ID}")
  fi
  tmp="$(mktemp)"
  trap 'rm -f "$tmp"' EXIT
  issue_uuid="$(resolve_issue_uuid "$tmp")"
  [[ -n "$issue_uuid" ]] || die "issue lookup returned no id"
  comment="$(python3 "$REDACT_PY" --comment <<EOF
Windows standalone GitHub Actions **pack** failed. No zip was published.

- Commit: \`${sha}\`
- Workflow: ${run_url}

Open that log for the compiler error. This is the API-off \`windows-standalone\` job (cargo-xwin on ubuntu-latest), not \`scripts/deploy.sh\` windows. A Clippy attachment-limit error is not this message.
EOF
)"
  status="$(curl -sS -o "$tmp" -w '%{http_code}' "${auth_args[@]}" \
    -H "Content-Type: application/json" \
    --data-binary "$comment" \
    "${API}/api/issues/${issue_uuid}/comments")"
  if [[ "$status" -lt 200 || "$status" -ge 300 ]]; then
    python3 -c 'import sys; print(sys.stdin.read()[:2000], file=sys.stderr)' < "$tmp" || true
    die "failure comment failed HTTP ${status}"
  fi
  log "posted windows-standalone failure comment on ${issue_id}"
  exit 0
fi

[[ -n "$FILE" ]] || die "missing zip path"
[[ -f "$FILE" ]] || die "missing file: $FILE"

name="$(basename "$FILE")"
title="${PAPERCLIP_ARTIFACT_TITLE:-Windows standalone ${name}}"
summary="${PAPERCLIP_ARTIFACT_SUMMARY:-API-off local-sim Windows sandbox. Extract with 7-Zip and the separately shared QA password, then run Play.cmd. Not the production patcher zip.}"
bytes="$(python3 -c 'import os,sys; print(os.path.getsize(sys.argv[1]))' "$FILE")"
download_url="$(github_zip_url)"

if [[ "$DRY_RUN" -eq 1 ]]; then
  printf 'api=%s\n' "$API"
  printf 'company=%s\n' "$COMPANY"
  printf 'issue=%s\n' "$issue_id"
  printf 'file=%s\n' "$FILE"
  printf 'bytes=%s\n' "$bytes"
  printf 'max_bytes=%s\n' "$max_bytes"
  printf 'title=%s\n' "$title"
  printf 'download_url=%s\n' "$download_url"
  if [[ "$bytes" -ge "$max_bytes" ]]; then
    printf 'mode=link\n'
  else
    printf 'mode=attachment\n'
  fi
  exit 0
fi

python3 "$(dirname "$0")/standalone_zip_crypto.py" verify "$FILE"

auth_args=(-H "Authorization: Bearer ${KEY}" -H "Accept: application/json")
if [[ -n "${PAPERCLIP_RUN_ID:-}" ]]; then
  auth_args+=(-H "X-Paperclip-Run-Id: ${PAPERCLIP_RUN_ID}")
fi

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

status="$(curl -sS -o "$tmp" -w '%{http_code}' "${auth_args[@]}" \
  "${API}/api/issues/${issue_id}")"
if [[ "$status" -lt 200 || "$status" -ge 300 ]]; then
  die "lookup issue ${issue_id} failed HTTP ${status}"
fi
issue_uuid="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' < "$tmp")"
issue_ident="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("identifier") or "")' < "$tmp")"
[[ -n "$issue_uuid" ]] || die "issue lookup returned no id"
log "paperclip issue ${issue_ident:-$issue_uuid}"

attachment_is_size_reject() {
  local http="$1"
  [[ "$http" == "413" ]] && return 0
  if [[ "$http" == "422" ]] && grep -qiE 'larger than|too large|10 MB|maximum' "$tmp"; then
    return 0
  fi
  return 1
}

post_github_link() {
  local reason="$1"
  [[ -n "$download_url" ]] || die "no GitHub download URL for oversize zip (${reason})"
  log "skipping Paperclip attachment (${reason}); zip is ${bytes} bytes"
  local wp
  wp="$(python3 -c 'import json,sys; print(json.dumps({
    "type": "artifact",
    "provider": "github",
    "title": sys.argv[1],
    "status": "ready_for_review",
    "reviewState": "none",
    "isPrimary": False,
    "healthStatus": "unknown",
    "summary": sys.argv[2],
    "metadata": {"url": sys.argv[3], "bytes": int(sys.argv[4]), "reason": sys.argv[5], "excludeFromLearning": True},
  }))' "$title" "$summary GitHub Releases (Paperclip attachments max 10 MB). Not a verified Windows run; excluded from fleet learning." "$download_url" "$bytes" "$reason" | python3 "$REDACT_PY" --json)"
  status="$(curl -sS -o "$tmp" -w '%{http_code}' "${auth_args[@]}" \
    -H "Content-Type: application/json" \
    --data-binary "$wp" \
    "${API}/api/issues/${issue_uuid}/work-products")"
  local wp_id=""
  if [[ "$status" -lt 200 || "$status" -ge 300 ]]; then
    log "warning: work-product create failed HTTP ${status}"
    python3 -c 'import sys; print(sys.stdin.read()[:2000], file=sys.stderr)' < "$tmp" || true
  else
    wp_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("id") or "")' < "$tmp")"
    log "created artifact work product ${wp_id}"
  fi
  if [[ "$NO_COMMENT" -eq 0 ]]; then
    local comment
    comment="$(python3 "$REDACT_PY" --comment <<EOF
Windows standalone sandbox is ready. Paperclip attachments are limited to 10 MB, so the encrypted zip is at the download link below.

- File: \`${name}\` (${bytes} bytes)
- Commit: \`${sha}\`
- Download: ${download_url}
- Workflow: ${run_url}

Extract with 7-Zip and the separately shared QA password, then run \`Play.cmd\`. This is the API-off local-sim client, not \`scripts/deploy.sh\` windows. Do not treat this zip as a verified Windows run or a fleet-learning example.
EOF
)"
    status="$(curl -sS -o "$tmp" -w '%{http_code}' "${auth_args[@]}" \
      -H "Content-Type: application/json" \
      --data-binary "$comment" \
      "${API}/api/issues/${issue_uuid}/comments")"
    if [[ "$status" -lt 200 || "$status" -ge 300 ]]; then
      python3 -c 'import sys; print(sys.stdin.read()[:2000], file=sys.stderr)' < "$tmp" || true
      die "issue comment failed HTTP ${status}"
    fi
  fi
  printf 'attachment_id=\n'
  printf 'work_product_id=%s\n' "$wp_id"
  printf 'download_path=%s\n' "$download_url"
  printf 'mode=link\n'
  printf 'issue=%s\n' "${issue_ident:-$issue_uuid}"
}

if [[ "$bytes" -ge "$max_bytes" ]]; then
  post_github_link "over ${max_bytes} bytes"
  exit 0
fi

status="$(curl -sS -o "$tmp" -w '%{http_code}' --max-time 3600 \
  "${auth_args[@]}" \
  -F "file=@${FILE};type=application/zip;filename=${name}" \
  "${API}/api/companies/${COMPANY}/issues/${issue_uuid}/attachments")"
if attachment_is_size_reject "$status"; then
  python3 -c 'import sys; print(sys.stdin.read()[:2000], file=sys.stderr)' < "$tmp" || true
  post_github_link "HTTP ${status} attachment limit"
  exit 0
fi
if [[ "$status" -lt 200 || "$status" -ge 300 ]]; then
  python3 -c 'import sys; print(sys.stdin.read()[:2000], file=sys.stderr)' < "$tmp" || true
  die "attachment upload failed HTTP ${status}"
fi
attachment_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("id") or "")' < "$tmp")"
download_path="$(python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("downloadPath") or ((d.get("contentPath") or "") + "?download=1"))' < "$tmp")"
[[ -n "$attachment_id" ]] || die "upload response missing attachment id"
log "uploaded attachment ${attachment_id}"

wp="$(python3 -c 'import json,sys; print(json.dumps({
  "type": "artifact",
  "provider": "paperclip",
  "title": sys.argv[1],
  "status": "ready_for_review",
  "reviewState": "none",
  "isPrimary": False,
  "healthStatus": "unknown",
  "summary": sys.argv[2],
  "metadata": {"attachmentId": sys.argv[3], "excludeFromLearning": True},
}))' "$title" "$summary Not a verified Windows run; excluded from fleet learning." "$attachment_id" | python3 "$REDACT_PY" --json)"
status="$(curl -sS -o "$tmp" -w '%{http_code}' "${auth_args[@]}" \
  -H "Content-Type: application/json" \
  --data-binary "$wp" \
  "${API}/api/issues/${issue_uuid}/work-products")"
if [[ "$status" -lt 200 || "$status" -ge 300 ]]; then
  python3 -c 'import sys; print(sys.stdin.read()[:2000], file=sys.stderr)' < "$tmp" || true
  die "work-product create failed HTTP ${status}"
fi
wp_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("id") or "")' < "$tmp")"
log "created artifact work product ${wp_id}"

if [[ "$NO_COMMENT" -eq 0 ]]; then
  comment="$(python3 "$REDACT_PY" --comment <<EOF
Windows standalone sandbox is on this issue as a Clippy artifact.

- File: \`${name}\`
- Commit: \`${sha}\`
- Download from this issue Output / company Artifacts (not Hostinger disk).
- Workflow: ${run_url}

Extract with 7-Zip and the separately shared QA password, then run \`Play.cmd\`. This is the API-off local-sim client, not \`scripts/deploy.sh\` windows. Do not treat this zip as a verified Windows run or a fleet-learning example.
EOF
)"
  status="$(curl -sS -o "$tmp" -w '%{http_code}' "${auth_args[@]}" \
    -H "Content-Type: application/json" \
    --data-binary "$comment" \
    "${API}/api/issues/${issue_uuid}/comments")"
  if [[ "$status" -lt 200 || "$status" -ge 300 ]]; then
    log "warning: issue comment failed HTTP ${status}"
  fi
fi

printf 'attachment_id=%s\n' "$attachment_id"
printf 'work_product_id=%s\n' "$wp_id"
printf 'download_path=%s\n' "$download_path"
printf 'mode=attachment\n'
printf 'issue=%s\n' "${issue_ident:-$issue_uuid}"
