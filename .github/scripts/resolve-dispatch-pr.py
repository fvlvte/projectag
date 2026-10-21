#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

_SHA = re.compile(r"[0-9a-fA-F]{40}")


def fetch_pr(number: str, repo: str) -> tuple[dict | None, int, str]:
    proc = subprocess.run(
        [
            "gh", "pr", "view", number, "--repo", repo,
            "--json", "number,headRefOid,baseRefOid,title,body",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None, proc.returncode or 1, proc.stderr or proc.stdout or ""
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None, 1, "dispatch PR JSON was not readable"
    if not isinstance(payload, dict):
        return None, 1, "dispatch PR JSON was not an object"
    return payload, 0, ""


def rejection(pr: dict, expected_sha: str) -> str | None:
    head = str(pr.get("headRefOid") or "")
    base = str(pr.get("baseRefOid") or "")
    if head != expected_sha:
        return f"dispatched sha {expected_sha[:12]} is not PR head {head[:12]}"
    if not _SHA.fullmatch(base):
        return "dispatch PR has no base sha"
    return None


def _marker(value: str) -> str:
    marker = "DISPATCH_EOF"
    while f"\n{marker}\n" in f"\n{value}\n":
        marker += "_X"
    return marker


def _multiline(name: str, value: str) -> str:
    marker = _marker(value)
    return f"{name}<<{marker}\n{value}\n{marker}\n"


def github_output(number: str, base: str, head: str, title: str, body: str) -> str:
    return (
        f"number={number}\n"
        f"base_sha={base}\n"
        f"head_sha={head}\n"
        + _multiline("title", title)
        + _multiline("body", body)
    )


def main() -> int:
    number = os.environ.get("DISPATCH_PR", "").strip()
    expected = os.environ.get("DISPATCH_SHA", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "fvlvte/projectag")
    if not number.isdecimal():
        print("dispatch pr number is not an integer", file=sys.stderr)
        return 1
    if not _SHA.fullmatch(expected):
        print("dispatch sha is not a commit", file=sys.stderr)
        return 1
    pr, code, err = fetch_pr(number, repo)
    if pr is None:
        if err:
            sys.stderr.write(err if err.endswith("\n") else err + "\n")
        return code or 1
    reason = rejection(pr, expected)
    if reason:
        print(reason, file=sys.stderr)
        return 1
    out = os.environ.get("GITHUB_OUTPUT", "")
    if not out:
        print("GITHUB_OUTPUT is unset", file=sys.stderr)
        return 1
    title = pr.get("title") or ""
    body = pr.get("body") or ""
    if not isinstance(title, str) or not isinstance(body, str):
        print("dispatch PR title or body was not text", file=sys.stderr)
        return 1
    with open(out, "a", encoding="utf-8") as fh:
        fh.write(github_output(number, str(pr["baseRefOid"]), str(pr["headRefOid"]), title, body))
    print(f"dispatch PR #{number} head {expected[:12]} matches")
    return 0


if __name__ == "__main__":
    sys.exit(main())
