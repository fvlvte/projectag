#!/usr/bin/env python3
"""Rebase every open, same-repo PR branch onto a newly pushed master.

Runs from .github/workflows/pr-rebase-sweep.yml on every push to `master`
(and on workflow_dispatch). This is pure git plumbing: a branch that already
sits on the new master is not rebased; one that rebases with no conflicts is
force-pushed (with lease) straight back onto itself. A branch that hits a
conflict is left untouched (`git rebase --abort`) and only logged
here. A run counts for that pull request only when it is a workflow_dispatch
whose pr_number input matches, or when it is a pull_request run for that
head that is not action_required.

The push does NOT start the PR's checks by itself. GitHub suppresses workflow
triggers for anything pushed with the automatic `GITHUB_TOKEN`, so the rebased
branch is left with no checks until this sweep dispatches them. The dispatch
passes `pr_number` so the replacement run can resolve that PR.
`qa-windows-harness` is dispatched only for a PR that carries the
`qa-windows` label, because its own jobs run unconditionally on
`workflow_dispatch` and would otherwise start a Windows build for every
rebased PR.

Conflicts are not reported to Paperclip (2026-09-17). Git-crypt makes
GitHub see every encrypted file as one opaque blob, so two PRs with disjoint
plaintext edits to the same file "conflict" on every sweep; the fleet used to
get a `CI infra: PR #<n> rebase conflict with master` issue per PR per master
push, each becoming a Lead run, a child issue, an implementer run and a human
review round for a rebase that the next master push undid. Such a PR is
merged from a laptop with `cargo agx repo merge <n>` instead: the git-crypt
driver merges encrypted files as text, a real conflict goes to a coding CLI or,
as a `Resolve conflicts:` issue, directly to a fleet lane, and
`cargo agx repo prs` lists what is clean and what still needs a hand
(docs/development/agx.md, "Repo").

Skipped: draft PRs, cross-repository (fork) PRs (GITHUB_TOKEN cannot push to
those), and branches already at the new master tip when every workflow they
should have dispatched already has a run for that pull request.

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
import re
import subprocess
import sys

REPO = os.environ.get("GITHUB_REPOSITORY", "fvlvte/projectag")
_PR_INPUT = re.compile(r"\bpr_number=([0-9]+)\b")
# Dispatched after a sweep push, because that push starts nothing on its own.
CHECK_WORKFLOWS = ("qa-linux.yml",)
LABELLED_WORKFLOWS = {"qa-windows": "qa-windows-harness.yml"}


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


def workflows_for(labels: list) -> list[str]:
    """Which workflows a rebased branch should have dispatched.

    Label-gated workflows run unconditionally on `workflow_dispatch`, so they
    are only dispatched for a PR that actually carries the label.
    """
    names = {l.get("name") if isinstance(l, dict) else l for l in labels or []}
    wanted = list(CHECK_WORKFLOWS)
    wanted += [wf for label, wf in LABELLED_WORKFLOWS.items() if label in names]
    return wanted


def dispatch_checks(
    number: int, branch: str, labels: list, only: list[str] | None = None
) -> list[str]:
    failed: list[str] = []
    selected = None if only is None else set(only)
    for workflow in workflows_for(labels):
        if selected is not None and workflow not in selected:
            continue
        out = run(
            "gh", "workflow", "run", workflow,
            "--repo", REPO,
            "--ref", branch,
            "-f", f"pr_number={number}",
            check=False,
        )
        if out.returncode == 0:
            print(f"PR #{number} {branch}: dispatched {workflow}")
        else:
            detail = (out.stderr or out.stdout or "").strip()[-200:]
            print(f"PR #{number} {branch}: could not dispatch {workflow}: {detail}")
            failed.append(workflow)
    return failed


def _listed_pr_number(row: dict) -> str:
    text = row.get("displayTitle")
    if isinstance(text, str):
        match = _PR_INPUT.search(text)
        if match:
            return match.group(1)
    return ""


def _run_covers_pr(row: dict, number: int, sha: str) -> bool:
    if row.get("headSha") != sha or row.get("conclusion") == "action_required":
        return False
    if row.get("event") == "pull_request":
        return True
    if row.get("event") != "workflow_dispatch":
        return False
    return _listed_pr_number(row) == str(number)


def workflows_without_runs(number: int, sha: str, workflows: list[str]) -> tuple[list[str], list[str]]:
    missing: list[str] = []
    unknown: list[str] = []
    for workflow in workflows:
        out = run(
            "gh", "run", "list",
            "--repo", REPO,
            "--workflow", workflow,
            "--commit", sha,
            "--limit", "30",
            "--json", "event,headSha,displayTitle,status,conclusion",
            check=False,
        )
        if out.returncode != 0:
            unknown.append(workflow)
            continue
        try:
            rows = json.loads(out.stdout or "[]")
        except json.JSONDecodeError:
            unknown.append(workflow)
            continue
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            unknown.append(workflow)
            continue
        if any(_run_covers_pr(row, number, sha) for row in rows):
            continue
        missing.append(workflow)
    return missing, unknown


def main() -> int:
    run("git", "fetch", "origin", "master")
    master = run("git", "rev-parse", "origin/master").stdout.strip()
    print(f"master is now {master[:12]}")

    prs = gh_json("pr", "list", "--repo", REPO, "--state", "open", "--json", "number,headRefName,isDraft,isCrossRepository,url,labels")
    rebased = skipped = conflicted = 0
    dispatch_failures: list[str] = []
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
            missing, unknown = workflows_without_runs(number, remote_sha, workflows_for(pr.get("labels")))
            if unknown:
                print(f"PR #{number} {branch}: could not list runs for {', '.join(unknown)}")
                dispatch_failures.extend(f"#{number} {workflow}" for workflow in unknown)
            if missing:
                print(
                    f"PR #{number} {branch}: already on {master[:12]}; "
                    f"dispatching {', '.join(missing)}"
                )
                dispatch_failures.extend(
                    f"#{number} {workflow}"
                    for workflow in dispatch_checks(number, branch, pr.get("labels"), only=missing)
                )
            elif not unknown:
                print(f"PR #{number} {branch}: already on {master[:12]}, checks present")
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
                dispatch_failures.extend(
                    f"#{number} {workflow}"
                    for workflow in dispatch_checks(number, branch, pr.get("labels"))
                )
                rebased += 1
        run("git", "worktree", "remove", "--force", wt, check=False)

    run("git", "worktree", "prune", check=False)
    print(
        f"\nrebased {rebased}, skipped {skipped}, conflicted {conflicted}, "
        f"dispatch failures {len(dispatch_failures)} of {len(prs)} open PR(s)"
    )
    if dispatch_failures:
        print("dispatch failed: " + ", ".join(dispatch_failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
