#!/usr/bin/env python3
"""Redact secrets and security internals from outbound Paperclip / GitHub text.

CI and agents run every comment, PR body and QA gate through ``redact`` before
it leaves the host. Secrets are never posted. Hasher / proof-of-work and
request-signing internals become ``[TOP SECRET]`` even on the company board
unless a human is looking at private Actions artifacts.

    python3 .github/scripts/paperclip-redact.py           # stdin -> stdout
    python3 .github/scripts/paperclip-redact.py --text T
    python3 .github/scripts/paperclip-redact.py --comment # stdin -> {"body": ...}
    python3 .github/scripts/paperclip-redact.py --json    # redact string values
"""

from __future__ import annotations

import argparse
import json
import re
import sys

MARKER = "[TOP SECRET]"

_ASSIGN_NAMES = (
    "GITCRYPT_KEY_B64",
    "R2_SECRET_ACCESS_KEY",
    "R2_ACCESS_KEY_ID",
    "PAPERCLIP_API_KEY",
    "PAPERCLIP_BOARD_API_KEY",
    "PAPERCLIP_SECRET_VALUE",
    "CURSOR_API_KEY",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "CLIENTS_DL_TOKEN",
    "PROJECTAG_NATIVE_SIGNING_KEY_FILE",
    "INBOX_R2_SECRET_ACCESS_KEY",
    "INBOX_R2_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
)

_ASSIGN = re.compile(
    r"(?i)\b(" + "|".join(re.escape(n) for n in _ASSIGN_NAMES) + r"|api[_-]?key|secret_access_key|private[_-]?key|signing[_-]?key|passwd|password)\b(\s*[=:]\s*)(\S+)"
)

_BEARER = re.compile(r"(?i)(\b(?:authorization|bearer)\b\s*[:=]?\s*(?:bearer\s+)?)([A-Za-z0-9._\-+/=]{12,})")

_SECRET_RES = [
    re.compile(
        r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----.*?-----END [A-Z0-9 ]{0,40}PRIVATE KEY-----",
        re.S,
    ),
    re.compile(r"\bghp_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bgho_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bghu_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
]

# Topic needles stay in this file (the filter). Agent copy must not teach them.
# Python's ``\b`` treats ``_`` as a word character, so it never fires inside a
# snake_case identifier -- exactly the shape a quoted Rust test or function name
# takes when an agent pastes one into a public PR body. Needles that can appear
# that way use an alnum-only boundary instead, so ``_word_`` still matches.
_NB = r"(?<![A-Za-z0-9])"
_NA = r"(?![A-Za-z0-9])"
_TOPIC_RES = [
    re.compile(r"(?i)randomx[-\w]*"),
    re.compile(r"(?i)xmrig"),
    re.compile(r"(?i)argon2\w*"),
    re.compile(rf"(?i){_NB}monero{_NA}"),
    re.compile(r"(?i)tevador"),
    re.compile(r"(?i)xworker-hash"),
    re.compile(r"(?i)kind_pow"),
    re.compile(r"(?i)proof[\s_-]+of[\s_-]+work"),
    re.compile(r"(?i)native-request-signing"),
    re.compile(r"(?i)projectag_request_signing"),
    re.compile(r"(?i)request-signing"),
    re.compile(r"(?i)x-projectag-request-(?:time|nonce|signature)"),
    re.compile(r"(?i)(?:^|[\s\"'`/(])hash\.rs\b"),
    # Underscore is a separator here (test names like foo_hasher_bar). Python
    # ``\b`` treats ``_`` as a word character and would miss those.
    re.compile(r"(?i)(?<![A-Za-z0-9])hasher(?![A-Za-z0-9])"),
    re.compile(rf"(?i){_NB}(?:librandomx|randomx-rs|randomx_rs){_NA}"),
]


def _assign_sub(match: re.Match[str]) -> str:
    return f"{match.group(1)}{match.group(2)}{MARKER}"


def _bearer_sub(match: re.Match[str]) -> str:
    return f"{match.group(1)}{MARKER}"


def redact(text: str | None) -> str:
    if not text:
        return ""
    out = text
    for rx in _SECRET_RES:
        out = rx.sub(MARKER, out)
    out = _ASSIGN.sub(_assign_sub, out)
    out = _BEARER.sub(_bearer_sub, out)
    for rx in _TOPIC_RES:
        out = rx.sub(MARKER, out)
    return out


def redact_obj(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact_obj(v) for v in value]
    if isinstance(value, dict):
        return {k: redact_obj(v) for k, v in value.items()}
    return value


def load_from_path(path) -> object:
    """Import this module from a filesystem path (CI bash heredocs)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("paperclip_redact", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--text", help="redact this string instead of stdin")
    ap.add_argument("--comment", action="store_true", help="print {\"body\": redacted}")
    ap.add_argument("--json", action="store_true", help="parse stdin as JSON and redact strings")
    args = ap.parse_args(argv)
    if args.text is not None:
        data = args.text
        parsed = None
    else:
        data = sys.stdin.read()
        parsed = None
        if args.json:
            parsed = json.loads(data)
    if parsed is not None:
        sys.stdout.write(json.dumps(redact_obj(parsed), ensure_ascii=False))
        return 0
    body = redact(data)
    if args.comment:
        sys.stdout.write(json.dumps({"body": body}, ensure_ascii=False))
    else:
        sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
