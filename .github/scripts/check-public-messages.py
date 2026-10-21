#!/usr/bin/env python3
"""Fail if a PR title, PR body, or commit message would leak on the public repo.

git-crypt encrypts file contents, never GitHub titles or squash bodies. A
GitHub squash merge publishes the PR title and body as the commit message.
This uses ``paperclip-redact.py`` as the needle list: if ``redact()`` changes
the text, the original must not be merged.

Failures name the field only. They do not print the original text.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location(
    "paperclip_redact", _HERE / "paperclip-redact.py"
)
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)
redact = _MOD.redact


def leaked(text: str | None) -> bool:
    if not text:
        return False
    return redact(text) != text


def git_messages(range_spec: str) -> list[tuple[str, str]]:
    if not range_spec:
        return []
    proc = subprocess.run(
        ["git", "log", "--format=%H%x00%s%n%b%x1e", range_spec],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(proc.returncode)
    out: list[tuple[str, str]] = []
    for chunk in proc.stdout.split("\x1e"):
        chunk = chunk.strip()
        if not chunk or "\x00" not in chunk:
            continue
        sha, message = chunk.split("\x00", 1)
        out.append((sha.strip(), message))
    return out


def collect(args: argparse.Namespace) -> list[tuple[str, str]]:
    fields: list[tuple[str, str]] = []
    title = args.title if args.title is not None else os.environ.get("PR_TITLE")
    body = args.body if args.body is not None else os.environ.get("PR_BODY")
    if title:
        fields.append(("PR title", title))
    if body:
        fields.append(("PR body", body))
    if args.text:
        fields.append(("message", args.text))
    if args.range:
        for sha, message in git_messages(args.range):
            fields.append((f"commit {sha[:12]}", message))
    return fields


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--title", help="PR title (else PR_TITLE)")
    ap.add_argument("--body", help="PR body (else PR_BODY)")
    ap.add_argument("--text", help="one extra message to scan")
    ap.add_argument("--range", help="git revision range whose messages to scan")
    args = ap.parse_args(argv)
    fields = collect(args)
    if not fields:
        print("public-message: nothing to scan")
        return 0
    hits = 0
    for label, text in fields:
        if leaked(text):
            print(f"public-message: {label} failed the public-repo scan")
            hits += 1
    if hits:
        print(
            "Rewrite the title/body without secrets or admission internals, "
            "then push. GitHub squash publishes that text as the commit message."
        )
        return 1
    print(f"public-message: {len(fields)} field(s) clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
