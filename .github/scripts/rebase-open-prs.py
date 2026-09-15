#!/usr/bin/env python3
"""Rebase every open, same-repo PR branch onto a newly pushed master.

Runs from .github/workflows/pr-rebase-sweep.yml on every push to `master`
(and on workflow_dispatch). This is pure git plumbing: a branch that already
sits on the new master is left alone; one that rebases with no conflicts is
force-pushed (with lease) straight back onto itself, so `qa-linux` /
`qa-windows-harness` re-run naturally via the `synchronize` event and no
agent is needed. A branch that hits a real conflict is left completely
untouched (`git rebase --abort`) and gets one comment on the Paperclip issue
resolved from its branch name, naming the conflicting paths — real conflict
resolution needs judgment, which is exactly what Harmony Lead's existing
"review existing PRs" fan-out already does (docs/development/paperclip-fleet.md).

Skipped: draft PRs, cross-repository (fork) PRs (GITHUB_TOKEN cannot push to
those), and branches already at the new master tip.

Concurrency note: this can race an agent actively pushing to the same
branch. `--force-with-lease` refuses the push if the remote moved since our
fetch, so the worst case is a skipped rebase this round, never a clobbered
push; an agent whose local checkout is now behind reconciles that the same
way it already does today (`cargo agx repo prove-rebase` / `rebase`).

Environment: GH_TOKEN (gh CLI + git push over the checkout's stored
credentials), PAPERCLIP_API_URL, PAPERCLIP_API_KEY, PAPERCLIP_COMPANY_ID —
the Paperclip ones are optional; without PAPERCLIP_API_KEY, conflicts are
still detected and left alone, just not reported anywhere.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("paperclip_redact", HERE / "paperclip-redact.py")
_redact_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_redact_mod)  # type: ignore[union-attr]
redact = _redact_mod.redact

REPO = os.environ.get("GITHUB_REPOSITORY", "fvlvte/projectag")
API = (os.environ.get("PAPERCLIP_API_URL") or "https://slategray-dolphin-616144.hostingersite.com").rstrip("/")
KEY = os.environ.get("PAPERCLIP_API_KEY", "")
COMPANY = os.environ.get("PAPERCLIP_COMPANY_ID", "")


def run(*args: str, check: bool = True, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=check, cwd=cwd, text=True, capture_output=True)


def gh_json(*args: str):
    out = run("gh", *args)
    return json.loads(out.stdout)


def paperclip(method: str, path: str, body: dict | None = None):
    if not KEY:
        return None, None
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        API + path,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {KEY}", "Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:400]


def resolve_issue(branch: str):
    """Same lookup as upload-paperclip-qa-summary.sh: identifier from the
    branch name, redirected to the one active child when the branch issue
    itself is not the one currently being worked."""
    m = re.search(r"([A-Za-z]+-[0-9]+)", branch)
    if not m:
        return None
    ident = m.group(1).upper()
    status, issue = paperclip("GET", f"/api/issues/{ident}")
    if not isinstance(issue, dict) or (status or 500) >= 300:
        return None
    if COMPANY and issue.get("status") not in ("in_progress", "in_review"):
        st, issues = paperclip("GET", f"/api/companies/{COMPANY}/issues")
        if st and st < 300 and isinstance(issues, list):
            active = [i for i in issues if i.get("parentId") == issue["id"] and i.get("status") in ("in_progress", "in_review")]
            if len(active) == 1:
                return active[0]
    return issue


def flag_conflict(branch: str, number: int, pr_url: str, paths: list[str]) -> None:
    issue = resolve_issue(branch)
    if not issue:
        print(f"  no Paperclip issue for branch {branch}; conflict not reported", file=sys.stderr)
        return
    body = redact("\n".join([
        f"Master moved and PR #{number} ({branch}) no longer rebases clean: {pr_url}",
        "Conflicting paths: " + (", ".join(paths) if paths else "(binary/encrypted content; check the branch directly)"),
        "The rebase sweep left the branch untouched (`git rebase --abort`) rather than guess a resolution — this needs a real edit.",
    ]))
    status, resp = paperclip("POST", f"/api/issues/{issue['id']}/comments", {"body": body})
    if status and status < 300:
        print(f"  flagged {issue.get('identifier')}")
    else:
        print(f"  could not comment on {issue.get('identifier')}: HTTP {status} {resp}", file=sys.stderr)


def main() -> int:
    run("git", "fetch", "origin", "master")
    master = run("git", "rev-parse", "origin/master").stdout.strip()
    print(f"master is now {master[:12]}")

    prs = gh_json("pr", "list", "--repo", REPO, "--state", "open", "--json", "number,headRefName,isDraft,isCrossRepository,url")
    rebased = skipped = conflicted = 0
    for pr in prs:
        branch, number = pr["headRefName"], pr["number"]
        if pr.get("isDraft"):
            print(f"PR #{number} {branch}: skip (draft)")
            skipped += 1
            continue
        if pr.get("isCrossRepository"):
            print(f"PR #{number} {branch}: skip (fork; GITHUB_TOKEN cannot push there)")
            skipped += 1
            continue

        run("git", "fetch", "origin", branch)
        remote_sha = run("git", "rev-parse", f"origin/{branch}").stdout.strip()
        base = run("git", "merge-base", remote_sha, master, check=False).stdout.strip()
        if base == master:
            print(f"PR #{number} {branch}: already on {master[:12]}, nothing to do")
            skipped += 1
            continue

        wt = f"/tmp/rebase-sweep-{number}"
        run("rm", "-rf", wt, check=False)
        run("git", "worktree", "prune", check=False)
        run("git", "worktree", "add", "--detach", "--quiet", wt, remote_sha)
        result = run("git", "-C", wt, "rebase", "origin/master", check=False)
        if result.returncode != 0:
            conflict_paths = run("git", "-C", wt, "diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
            run("git", "-C", wt, "rebase", "--abort", check=False)
            print(f"PR #{number} {branch}: real conflict ({len(conflict_paths)} path(s)); left untouched")
            flag_conflict(branch, number, pr.get("url", ""), conflict_paths)
            conflicted += 1
        else:
            push = run(
                "git", "-C", wt, "push", f"--force-with-lease={branch}:{remote_sha}",
                "origin", f"HEAD:refs/heads/{branch}", check=False,
            )
            if push.returncode != 0:
                print(f"PR #{number} {branch}: rebased clean but the branch moved concurrently; left as-is\n{push.stderr[-500:]}")
                skipped += 1
            else:
                print(f"PR #{number} {branch}: rebased onto {master[:12]} and pushed")
                rebased += 1
        run("git", "worktree", "remove", "--force", wt, check=False)

    run("git", "worktree", "prune", check=False)
    print(f"\nrebased {rebased}, skipped {skipped}, conflicted {conflicted} of {len(prs)} open PR(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
