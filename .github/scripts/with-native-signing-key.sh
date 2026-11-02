#!/usr/bin/env bash
# Runs one command with the native request-signing key the shipped binaries
# embed (build.rs reads PROJECTAG_NATIVE_SIGNING_KEY_FILE). The key exists as
# a file only while that command runs, and its path is set for that command
# alone, never exported to the job environment, so its other steps (toolchain
# and dependency installs, uploads) cannot read it. Never echo the key.
#
#   NATIVE_KEY_HEX=<64 hex> with-native-signing-key.sh COMMAND [ARGS...]
set -euo pipefail
if [ "$#" -eq 0 ]; then
  echo "::error::with-native-signing-key.sh needs a command"
  exit 2
fi
if [ -z "${NATIVE_KEY_HEX:-}" ]; then
  echo "::error::NATIVE_KEY_HEX (PROJECTAG_NATIVE_SIGNING_KEY_HEX) is empty"
  exit 1
fi
umask 077
# A fixed path keeps build.rs's rerun-if-env-changed input stable, so a
# restored build cache is not invalidated by a new temporary name.
key="${RUNNER_TEMP:?RUNNER_TEMP is unset}/native-request-signing.key"
trap 'rm -f "$key"' EXIT
rm -f "$key"
printf %s "$NATIVE_KEY_HEX" | xxd -r -p > "$key"
unset NATIVE_KEY_HEX
test "$(wc -c < "$key" | tr -d ' ')" = 32 || { echo "::error::PROJECTAG_NATIVE_SIGNING_KEY_HEX is not 32 bytes of hex"; exit 1; }
status=0
PROJECTAG_NATIVE_SIGNING_KEY_FILE="$key" "$@" || status=$?
exit "$status"
