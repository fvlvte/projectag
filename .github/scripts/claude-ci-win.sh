#!/usr/bin/env bash
# claude-ci run step on Windows (Git Bash). Decrypts the hand-off tree, restores the encrypted build cache (the
# "cache" artifact of the run that .claude-ci/meta.json names), runs .claude-ci/cmd.sh, saves a new encrypted
# cache when meta.json asks for one, and encrypts the results to the developer's age key. Prints NOTHING to the
# job log.
set +x +e
T="$(cygpath -u "$RUNNER_TEMP")"; WS="$(cygpath -u "$GITHUB_WORKSPACE")"
R="$T/claude-ci"; mkdir -p "$R/results" "$R/ws"
exec > "$R/results/runner.log" 2>&1
set -u -o pipefail
umask 077
AGE="$T/age/age/age.exe"
ENC=(enc -aes-256-cbc -pbkdf2 -iter 200000 -pass env:GITCRYPT_KEY_B64)
cd "$R/ws"
openssl "${ENC[@]}" -d -in "$T/src/src.enc" | tar -xzf - && echo "source unpacked"
rm -f "$T/src/src.enc"
meta() { python -c 'import json, sys; v = json.load(open(".claude-ci/meta.json")).get(sys.argv[1]); print("" if v is None else (str(v).lower() if isinstance(v, bool) else v))' "$1"; }
cache_run="$(meta cache_run)"; save="$(meta save_cache)"; sub="$(meta sub)"
# registry crates are not cached here: their freshness never depends on mtimes, so cargo re-downloads them
export CARGO_TARGET_DIR="$(cygpath -w "$R/ws/target")" CARGO_BUILD_JOBS="$NUMBER_OF_PROCESSORS" CARGO_TERM_COLOR=never CARGO_INCREMENTAL=0
if [ -n "$cache_run" ]; then
  mkdir -p "$R/cache"
  if gh run download "$cache_run" -R "$GITHUB_REPOSITORY" -n cache -D "$(cygpath -w "$R/cache")"; then
    openssl "${ENC[@]}" -d -in "$R/cache/ci-cache.enc" | tar -xzf - -C "$R/ws" && echo "cache restored from run $cache_run"
  else
    echo "cache download from run $cache_run failed"
  fi
  rm -rf "$R/cache"
fi
cd "$R/ws/$sub"
start=$(date +%s)
bash "$R/ws/.claude-ci/cmd.sh" > "$R/results/out.log" 2>&1
echo $? > "$R/results/rc"
echo $(( $(date +%s) - start )) > "$R/results/seconds"
[ -d "$R/ws/.claude-ci/logs" ] && cp -r "$R/ws/.claude-ci/logs" "$R/results/"
if [ "$save" = true ]; then
  rm -rf "$R/ws/target/debug/incremental"
  tar -C "$R/ws" -czf - target | openssl "${ENC[@]}" -salt -out "$WS/ci-cache.enc" && stat -c %s "$WS/ci-cache.enc" > "$R/results/cache_bytes"
fi
df -h > "$R/results/df.txt" 2>&1
exec > /dev/null 2>&1
tar -C "$R" -czf - results | "$AGE" -r "$RESULTS_RECIPIENT" > "$WS/results.age"
exit 0
