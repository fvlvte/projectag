#!/usr/bin/env bash
# Upload the Windows standalone zip to Cloudflare R2 and delete older zips so
# the prefix stays at or under 10 GB (Cloudflare's free storage).
#
# GitHub private-repo asset redirects expire in about five minutes
# (X-Amz-Expires=300). R2 is the durable anonymous download for Clippy.
#
#   .github/scripts/publish-r2-windows-zip.sh FILE.zip [TAG]
#   .github/scripts/publish-r2-windows-zip.sh --dry-run FILE.zip [TAG]
#
# Env: R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY. Optional: R2_ACCOUNT_ID
#      (default d9513a4189039ef82fa87e6a03465344), R2_BUCKET
#      (default engine-windows-standalone), R2_PUBLIC_BASE_URL
#      (default https://builds.projectag.online), R2_PREFIX
#      (default windows-standalone), R2_MAX_BYTES (default 10000000000),
#      R2_ENDPOINT, GITHUB_OUTPUT.
set -euo pipefail

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

HERE="$(cd "$(dirname "$0")" && pwd)"
PY="$HERE/r2_windows_zip.py"
[[ -f "$PY" ]] || die "missing $PY"
command -v python3 >/dev/null || die "python3 is not on PATH"

exec python3 "$PY" "$@"
