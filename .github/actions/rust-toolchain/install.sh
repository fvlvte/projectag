#!/usr/bin/env bash
# Install the pinned Rust toolchain. When an install fails, remove what it left
# behind, wait, and retry, at most ATTEMPTS times in all. After the last failure
# this exits with rustup's status.
#
#   install.sh TOOLCHAIN [TARGETS] [COMPONENTS]
#
# TARGETS and COMPONENTS are comma- or space-separated, like the inputs of
# dtolnay/rust-toolchain. The flags match that action's install, so the action
# that runs next (action.yml) finds the toolchain present and changes nothing.
#
# Why: windows-standalone runs 44823093279 (2026-11-20) and 44884441687
# (2026-11-21) found a partly installed toolchain on their first install
# ("recovering from a partially installed toolchain"). They failed with
# "failed to install component: 'clippy-preview-...', detected conflict:
# 'bin/cargo-clippy'". dtolnay retries only checksum errors. Retrying on top of
# the leftover files hits the same conflict; the reruns on new runners passed.
set -euo pipefail

ATTEMPTS=3
# Seconds to wait before attempts 2 and 3.
BACKOFF=(20 60)

toolchain=${1:?usage: install.sh TOOLCHAIN [TARGETS] [COMPONENTS]}
targets=${2:-}
components=${3:-}

if ! command -v rustup >/dev/null 2>&1; then
  # dtolnay/rust-toolchain installs rustup and then the toolchain itself.
  echo "rustup is not on PATH; dtolnay/rust-toolchain installs it and $toolchain"
  exit 0
fi

flags=(--profile minimal --no-self-update --force-non-host)
IFS=$', \t\n' read -r -a list <<<"$targets" || true
for t in ${list[@]+"${list[@]}"}; do flags+=(--target "$t"); done
IFS=$', \t\n' read -r -a list <<<"$components" || true
for c in ${list[@]+"${list[@]}"}; do flags+=(--component "$c"); done

rustup_home() {
  local home=${RUSTUP_HOME:-$HOME/.rustup}
  # Git Bash on Windows: RUSTUP_HOME may be a C:\ path.
  if command -v cygpath >/dev/null 2>&1; then home=$(cygpath -u "$home"); fi
  printf '%s\n' "$home"
}

# Remove the toolchain this script installs and nothing else. `rustup toolchain
# uninstall` can fail on a half-written toolchain, so the directory, its update
# hash and rustup's temporary files are removed as well.
clean_toolchain() {
  local home
  home=$(rustup_home)
  echo "::group::state of $toolchain before the clean retry"
  ls -la "$home/toolchains" 2>/dev/null || true
  # Another rustup, cargo or rustc running here would explain a partial install.
  ps -eo pid,ppid,etime,comm 2>/dev/null | grep -E '(^|[ /])(rustup|cargo|rustc)$' || true
  echo "::endgroup::"
  rustup toolchain uninstall "$toolchain" || echo "rustup toolchain uninstall $toolchain failed; removing its files"
  if [[ -z $home || $home == / ]]; then
    echo "::error::refusing to clean toolchain files under '$home'"
    return 1
  fi
  rm -rf "$home/toolchains/$toolchain"-* "$home/update-hashes/$toolchain"-* "$home/tmp"
}

for ((attempt = 1; attempt <= ATTEMPTS; attempt++)); do
  if rustup toolchain install "$toolchain" "${flags[@]}"; then
    exit 0
  else
    status=$?
  fi
  if ((attempt == ATTEMPTS)); then
    echo "::error::rustup toolchain install $toolchain failed $ATTEMPTS times, each from a clean toolchain directory"
    exit "$status"
  fi
  wait_s=${BACKOFF[attempt - 1]}
  echo "::warning::rustup toolchain install $toolchain failed (attempt $attempt of $ATTEMPTS, exit $status); removing the partial toolchain and retrying in ${wait_s}s"
  clean_toolchain
  sleep "$wait_s"
done
