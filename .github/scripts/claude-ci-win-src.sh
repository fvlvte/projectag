#!/usr/bin/env bash
# claude-ci Windows hand-off, run on Linux: unlock, delete real credentials, pack the tree encrypted with the
# GITCRYPT_KEY_B64 passphrase into $RUNNER_TEMP/src.enc for the Windows job. Prints nothing.
set +x +e
exec > /dev/null 2>&1
set -u -o pipefail
umask 077
cd "$GITHUB_WORKSPACE"
bash .github/scripts/git-crypt-unlock.sh
find backend/deploy -type f \( -name '*.env' -o -name '*.token' -o -name '*.password' -o -name '*.key' -o -path '*/certs/*' \) -delete
rm -f admin/src/config.ts backend/scripts/qa-hosted-client.py backend/scripts/test-projectag-email-setup.py
printf '%032d\n' 0 > backend/deploy/download.token; printf 'placeholder-0000\n' > backend/deploy/download.password; printf '%032d\n' 0 > backend/deploy/inbox-tool.token
# the release identity carries a real release seed: keep only its public version fields, swap the key for a fixed dummy (all-zero is rejected by ReleaseIdentity::parse)
python3 -c 'import json; p="backend/deploy/release-identity.json"; d=json.load(open(p)); d["signing_key"]="01"*32; open(p,"w").write(json.dumps(d))'
if [ ! -d vendor/native-ext ] && [ -f scripts/vendor-pack.py ]; then python3 scripts/vendor-pack.py ci-prepare; fi
tar --exclude=./.git -czf - . | openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass env:GITCRYPT_KEY_B64 -out "$RUNNER_TEMP/src.enc"
exit 0
