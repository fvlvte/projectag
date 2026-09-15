#!/usr/bin/env bash
# Debian/Ubuntu LLVM packages ship llvm-ar/clang/lld, not the MSVC-named
# shims cargo-xwin and cc-rs look for (llvm-lib, clang-cl, lld-link).
# LLVM's llvm-ar behaves as llvm-lib when argv0 is llvm-lib.
#
#   .github/scripts/install-xwin-msvc-shims.sh
#
# Links into ${XWIN_SHIM_DIR:-$HOME/.local/bin}. Put that directory on PATH
# before `cargo xwin build`.
set -euo pipefail

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

find_tool() {
  local c
  for c in "$@"; do
    if command -v "$c" >/dev/null 2>&1; then
      command -v "$c"
      return 0
    fi
  done
  return 1
}

DEST="${XWIN_SHIM_DIR:-${HOME}/.local/bin}"
mkdir -p "$DEST"

ar_src="$(find_tool llvm-ar llvm-ar-20 llvm-ar-19 llvm-ar-18 llvm-ar-17 \
  || true)"
clang_src="$(find_tool clang clang-20 clang-19 clang-18 clang-17 || true)"
lld_src="$(find_tool lld-link lld-link-20 lld-link-19 lld-link-18 \
  ld.lld ld.lld-20 ld.lld-19 ld.lld-18 || true)"

[[ -n "$ar_src" ]] || die "need llvm-ar (apt install llvm, or brew install llvm)"
[[ -n "$clang_src" ]] || die "need clang (apt install clang)"
[[ -n "$lld_src" ]] || die "need lld (apt install lld)"

ln -sfn "$ar_src" "$DEST/llvm-lib"
ln -sfn "$clang_src" "$DEST/clang-cl"
ln -sfn "$lld_src" "$DEST/lld-link"

printf 'shim_dir=%s\n' "$DEST"
printf 'llvm-lib -> %s\n' "$ar_src"
printf 'clang-cl -> %s\n' "$(readlink "$DEST/clang-cl" 2>/dev/null || echo "$DEST/clang-cl")"
printf 'lld-link -> %s\n' "$(readlink "$DEST/lld-link" 2>/dev/null || echo "$DEST/lld-link")"
command -v llvm-lib >/dev/null 2>&1 || PATH="$DEST:$PATH" command -v llvm-lib >/dev/null \
  || die "llvm-lib shim was written to $DEST but is not executable"
