#!/usr/bin/env bash
# Body of the claude-ci run step on Linux and macOS (branches claude/ci/** and claude/ci-mac/**; never master).
# It prints NOTHING to the job log. Everything goes to files inside the age-encrypted results artifact.
# Order: unlock, delete real credentials, restore the encrypted cache, run .claude-ci/cmd.sh (git-crypt
# encrypted in the repo), encrypt the results.
set +x +e
R="$RUNNER_TEMP/claude-ci"; mkdir -p "$R/results"
exec > "$R/results/runner.log" 2>&1
set -u -o pipefail
umask 077
if [ "$RUNNER_OS" = "macOS" ]; then
  OPENSSL="$(brew --prefix openssl@3)/bin/openssl"; NPROC="$(sysctl -n hw.ncpu)"; SIZE() { stat -f %z "$1"; }
else
  OPENSSL=openssl; NPROC="$(nproc)"; SIZE() { stat -c %s "$1"; }
fi
cd "$GITHUB_WORKSPACE"
restored=0; if [ -f ci-cache.enc ] && [ "$(SIZE ci-cache.enc)" -gt 300000000 ]; then restored=1; fi  # a tiny cache counts as none
bash .github/scripts/git-crypt-unlock.sh
# real credentials never take part in a run: delete them, then write dummy placeholders for build scripts
find backend/deploy -type f \( -name '*.env' -o -name '*.token' -o -name '*.password' -o -name '*.key' -o -path '*/certs/*' \) -delete 2>/dev/null
rm -f admin/src/config.ts backend/scripts/qa-hosted-client.py backend/scripts/test-projectag-email-setup.py
printf '%032d\n' 0 > backend/deploy/download.token; printf 'placeholder-0000\n' > backend/deploy/download.password; printf '%032d\n' 0 > backend/deploy/inbox-tool.token
# the release identity carries a real release seed: keep only its public version fields, zero the key
python3 -c 'import json; p="backend/deploy/release-identity.json"; d=json.load(open(p)); d["signing_key"]="0"*64; open(p,"w").write(json.dumps(d))'
if [ -f ci-cache.enc ]; then
  "$OPENSSL" enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass env:GITCRYPT_KEY_B64 -in ci-cache.enc | zstd -dq | tar -x -C / && echo "cache restored"
  rm -f ci-cache.enc
fi
if [ ! -d vendor/native-ext ] && [ -f scripts/vendor-pack.py ]; then python3 scripts/vendor-pack.py ci-prepare; fi
sub="$(jq -r '.sub // ""' .claude-ci/meta.json)"
cd "$GITHUB_WORKSPACE/$sub"
export CARGO_BUILD_JOBS="$NPROC" CARGO_TERM_COLOR=never
start=$(date +%s)
bash "$GITHUB_WORKSPACE/.claude-ci/cmd.sh" > "$R/results/out.log" 2>&1
echo $? > "$R/results/rc"
echo $(( $(date +%s) - start )) > "$R/results/seconds"
[ -d "$GITHUB_WORKSPACE/.claude-ci/logs" ] && cp -r "$GITHUB_WORKSPACE/.claude-ci/logs" "$R/results/"
cd "$GITHUB_WORKSPACE"
if [ "$restored" = 0 ]; then
  rm -rf target/debug/incremental
  tar -c "$HOME/.cargo/registry/index" "$HOME/.cargo/registry/cache" "$GITHUB_WORKSPACE/target" 2>/dev/null \
    | zstd -q -T0 -3 | "$OPENSSL" enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass env:GITCRYPT_KEY_B64 -out ci-cache.enc
fi
exec > /dev/null 2>&1
tar -C "$R" -c results | zstd -q -T0 | age -r "$RESULTS_RECIPIENT" > "$GITHUB_WORKSPACE/results.age"
exit 0
