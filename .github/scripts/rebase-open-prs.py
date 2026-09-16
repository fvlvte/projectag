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
import tempfile
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


def _parse_body(raw):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return raw.decode(errors="replace")[:400] if isinstance(raw, (bytes, bytearray)) else str(raw)[:400]


def _paperclip_urllib(method: str, path: str, body: dict | None):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Authorization": f"Bearer {KEY}", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(API + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, _parse_body(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, _parse_body(e.read())
    except (urllib.error.URLError, OSError) as e:
        # No HTTP response at all (DNS, connect refused, ENETUNREACH); paperclip()
        # retries this with curl the same way it retries a 403.
        return 597, f"urllib-transport: {e}"


def _paperclip_curl(method: str, path: str, body: dict | None):
    """Same request shape as upload-paperclip-qa-summary.sh's curl fallback."""
    out = tempfile.NamedTemporaryFile(delete=False)
    out.close()
    data_path = None
    cmd = [
        "curl", "-sS", "-o", out.name, "-w", "%{http_code}", "-X", method,
        "-H", f"Authorization: Bearer {KEY}", "-H", "Accept: application/json", "--max-time", "60",
    ]
    try:
        if body is not None:
            with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as data_file:
                json.dump(body, data_file)
                data_path = data_file.name
            cmd += ["-H", "Content-Type: application/json", "--data-binary", f"@{data_path}"]
        cmd.append(API + path)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=70)
        try:
            status = int((proc.stdout or "").strip() or "0")
        except ValueError:
            status = 599
        payload = _parse_body(Path(out.name).read_bytes())
        return (599, "curl-transport") if proc.returncode != 0 and status < 300 else (status, payload)
    except FileNotFoundError:
        return 599, "curl-missing"
    finally:
        for p in (out.name, data_path):
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass


def paperclip(method: str, path: str, body: dict | None = None):
    """GitHub-hosted runners: Python urllib has returned HTTP 403 on a GET that
    curl with the same board key succeeds on seconds later (same finding as
    upload-paperclip-qa-summary.sh, PR #9, 2026-09-15), and has raised a raw
    transport error (status 597 from _paperclip_urllib, e.g. ENETUNREACH) on a
    runner where curl reached the same host in the same run. Retry both with curl."""
    if not KEY:
        return None, None
    status, payload = _paperclip_urllib(method, path, body)
    if status in (403, 597):
        curl_status, curl_payload = _paperclip_curl(method, path, body)
        print(f"Paperclip {method} {path} urllib HTTP {status}; retry curl HTTP {curl_status}", file=sys.stderr)
        return curl_status, curl_payload
    return status, payload


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


def find_lead_agent_id():
    if not COMPANY:
        return None
    st, agents = paperclip("GET", f"/api/companies/{COMPANY}/agents")
    if not st or st >= 300 or not isinstance(agents, list):
        return None
    return next((a.get("id") for a in agents if a.get("name") == "Harmony Lead"), None)


def find_qa_label_id():
    if not COMPANY:
        return None
    st, labels = paperclip("GET", f"/api/companies/{COMPANY}/labels")
    if not st or st >= 300 or not isinstance(labels, list):
        return None
    return next((l.get("id") for l in labels if l.get("name") == "qa"), None)


def find_open_issue_by_title(title: str):
    if not COMPANY:
        return None
    st, issues = paperclip("GET", f"/api/companies/{COMPANY}/issues")
    if not st or st >= 300 or not isinstance(issues, list):
        return None
    return next((i for i in issues if i.get("title") == title and i.get("status") not in ("done", "cancelled")), None)


def flag_conflict(branch: str, number: int, pr_url: str, paths: list[str]) -> None:
    """Comment on the branch issue for context, and — the part that actually
    gets someone to look — open (or reuse) a `CI infra:` issue assigned to
    Harmony Lead, the same convention QA Analyst already uses for gaps it
    cannot resolve itself. A comment alone never wakes anyone in this fleet
    (no agent is woken directly by CI); routing through an assignment does."""
    conflict_line = "Conflicting paths: " + (", ".join(paths) if paths else "(binary/encrypted content; check the branch directly)")
    issue = resolve_issue(branch)
    if issue:
        body = redact("\n".join([
            f"Master moved and PR #{number} ({branch}) no longer rebases clean: {pr_url}",
            conflict_line,
            "The rebase sweep left the branch untouched (`git rebase --abort`) rather than guess a resolution — this needs a real edit.",
            "Filed as a `CI infra:` issue for Harmony Lead so this gets routed to a lane instead of sitting here.",
        ]))
        status, resp = paperclip("POST", f"/api/issues/{issue['id']}/comments", {"body": body})
        if status and status < 300:
            print(f"  commented on {issue.get('identifier')}")
        else:
            print(f"  could not comment on {issue.get('identifier')}: HTTP {status} {resp}", file=sys.stderr)
    else:
        print(f"  no Paperclip issue for branch {branch}", file=sys.stderr)

    title = f"CI infra: PR #{number} rebase conflict with master"
    existing = find_open_issue_by_title(title)
    if existing:
        print(f"  {existing.get('identifier')} already open for this; not duplicating")
        return
    lead_id = find_lead_agent_id()
    qa_label = find_qa_label_id()
    body = redact("\n".join([
        f"The rebase sweep ({pr_url}) found a real conflict rebasing PR #{number} ({branch}) onto master.",
        conflict_line,
        "Left untouched (`git rebase --abort`). Route to the lane that owns the conflicting surface, same as any other `CI infra:` gap; never assign it to the original implementer directly.",
    ]))
    payload = {"title": title, "description": body, "status": "todo", "priority": "medium"}
    if lead_id:
        payload["assigneeAgentId"] = lead_id
    if qa_label:
        payload["labelIds"] = [qa_label]
    status, created = paperclip("POST", f"/api/companies/{COMPANY}/issues", payload) if COMPANY else (None, None)
    if status and status < 300 and isinstance(created, dict):
        print(f"  opened {created.get('identifier')} for Harmony Lead")
    else:
        print(f"  could not open a CI infra issue: HTTP {status} {created}", file=sys.stderr)


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
