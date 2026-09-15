#!/usr/bin/env bash
# Unlock a git-crypt clone when `.gitattributes` itself is encrypted.
# This file lives under `.github/` so GitHub Actions receives plaintext
# YAML/scripts; `actions/checkout` cannot run git-crypt smudge by itself.
# Set GITCRYPT_KEY_B64 to the `git-crypt export-key` payload, base64-encoded.
# Do not print the key.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[[ -n "${GITCRYPT_KEY_B64:-}" ]] || die "GITCRYPT_KEY_B64 is unset — add it as a GitHub Actions secret"
command -v git-crypt >/dev/null || die "git-crypt is not on PATH"
command -v git >/dev/null || die "git is not on PATH"
command -v python3 >/dev/null || die "python3 is not on PATH"

COMMON="$(git rev-parse --git-common-dir)"
mkdir -p "$COMMON/info" "$COMMON/git-crypt/keys"
# `.gitattributes` is ciphertext, so git-crypt cannot discover patterns from
# the worktree. Local-only attributes make every path go through smudge.
# `.github/**` stays plaintext so GitHub Actions can read workflow YAML.
printf '%s\n' \
  '**/* filter=git-crypt diff=git-crypt' \
  '.github/** !filter !diff' \
  > "$COMMON/info/attributes"
git config filter.git-crypt.smudge '"git-crypt" smudge'
git config filter.git-crypt.clean '"git-crypt" clean'
git config filter.git-crypt.required true

key="$COMMON/git-crypt/keys/default"
umask 077
printf %s "$GITCRYPT_KEY_B64" | python3 -c 'import sys,base64; sys.stdout.buffer.write(base64.b64decode(sys.stdin.read()))' > "$key"
chmod 600 "$key"

git ls-files -z | xargs -0 rm -f
git checkout -f HEAD

python3 - <<'PY'
from pathlib import Path
p = Path("AGENTS.md")
if not p.is_file():
    raise SystemExit("unlock failed: AGENTS.md missing")
start = p.read_bytes()[:24]
if not start.startswith(b"#"):
    raise SystemExit("unlock failed: AGENTS.md is still ciphertext")
print("git-crypt: worktree is plaintext")
PY

# Prepare the path crate Cargo expects at vendor/native-ext.
# On GitHub Actions this writes a stub and wraps `cargo` so public logs do
# not compile or name the real tree. Laptops unpack the blob. The helper
# lives under scripts/ (encrypted), not .github/.
if [[ -f scripts/vendor-pack.py ]]; then
  python3 scripts/vendor-pack.py ci-prepare
fi
