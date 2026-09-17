#!/usr/bin/env python3
"""Rebase every open, same-repo PR branch onto a newly pushed master.

Runs from .github/workflows/pr-rebase-sweep.yml on every push to `master`
(and on workflow_dispatch). This is pure git plumbing: a branch that already
sits on the new master is left alone; one that rebases with no conflicts is
force-pushed (with lease) straight back onto itself, so `qa-linux` /
`qa-windows-harness` re-run naturally via the `synchronize` event and no
agent is needed. A branch that hits a conflict is left completely untouched
(`git rebase --abort`) and only logged here.

Conflicts are not reported to Paperclip any more (2026-09-17). Git-crypt makes
GitHub see every encrypted file as one opaque blob, so two PRs with disjoint
plaintext edits to the same file "conflict" on every sweep; the fleet used to
get a `CI infra: PR #<n> rebase conflict with master` issue per PR per master
push, each becoming a Lead run, a child issue, an implementer run and a human
review round for a rebase that the next master push undid again. Such a PR is
merged from a laptop with `cargo agx repo merge <n>` instead: the git-crypt
driver merges encrypted files as text, a real conflict goes to a coding CLI or,
as a `Resolve conflicts:` issue, directly to a fleet lane, and
`cargo agx repo prs` lists what is clean and what still needs a hand
(docs/development/agx.md, "Repo").

Skipped: draft PRs, cross-repository (fork) PRs (GITHUB_TOKEN cannot push to
those), and branches already at the new master tip.

Concurrency note: this can race an agent actively pushing to the same
branch. `--force-with-lease` refuses the push if the remote moved since our
fetch, so the worst case is a skipped rebase this round, never a clobbered
push; an agent whose local checkout is now behind reconciles that the same
way it already does today (`cargo agx repo prove-rebase` / `rebase`).

Environment: GH_TOKEN (gh CLI + git push over the checkout's stored
credentials). No Paperclip credentials are needed or read.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

REPO = os.environ.get("GITHUB_REPOSITORY", "fvlvte/projectag")


def run(*args: str, check: bool = True, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=check, cwd=cwd, text=True, capture_output=True)


def gh_json(*args: str):
    out = run("gh", *args)
    return json.loads(out.stdout)


def conflict_note(number: int, branch: str, paths: list[str]) -> str:
    """One log line per conflicted PR; the laptop merge is the fix, not an issue."""
    listed = ", ".join(paths) if paths else "(binary/encrypted content; check the branch directly)"
    return (
        f"PR #{number} {branch}: conflict on {len(paths)} path(s); left untouched. "
        f"Merge it from a laptop: `cargo agx repo merge {number}` (encrypted files merge as text there). "
        f"Paths: {listed}"
    )


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
            print(conflict_note(number, branch, conflict_paths))
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
