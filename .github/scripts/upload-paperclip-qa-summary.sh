#!/usr/bin/env bash
# Post one consolidated QA gate comment per workflow run to the Paperclip issue
# for this branch, and advance the issue to its review stages when every
# required run is green.
#
#   upload-paperclip-qa-summary.sh [qa-report.json ...]
#
# Zero reports is valid (a job failed before it produced evidence): the gate
# line is still posted. Evidence stays in private Actions artifacts; the
# comment names them. Do not reuse upload-paperclip-artifact.sh here: that
# helper describes a downloadable Windows zip, which this report is not.
#
# Contract (docs/development/paperclip-fleet.md, "CI gate and evidence"):
#   * the comment starts with `QA gate: pass|fail|pending` and a `Required:`
#     line naming every required workflow for the PR head
#   * pass  + issue `in_progress` + label `awaiting-ci` + unchanged PR head
#           -> the label is removed and the issue is set `done` with this
#              comment; the runtime turns that into `in_review`
#   * fail  -> the comment is posted (it wakes the implementer); the label is
#              removed so a later green run cannot advance stale work
#   * pending (another required workflow is still running) -> nothing is
#              posted; the workflow that finishes last posts the gate
#   * PAPERCLIP_NIGHTLY=1 -> the summaries go to the standing issue
#     `QA nightly` (created when missing), never to a branch issue
#
# Issue lookup: resolve_issue() GET /api/issues/{ident} from the branch name
# (`AGS-12-...`). When the branch has no `{PREFIX}-{n}` token and
# PAPERCLIP_PR_NUMBER is set, it selects the issue that already has a
# pull_request work product for that PR (`GET /api/issues/{id}/work-products`,
# `externalId` or `/pull/{n}` URL). GitHub-hosted Python urllib has returned
# HTTP 403 on issue GET while curl with the same board key succeeded
# (upload-paperclip-artifact.sh); it has also raised a raw transport error
# (URLError/OSError, "Network is unreachable") on a GET a same-run curl
# completed. paperclip() retries both with curl and logs
# the response class (json-issue / json-error / html / text) without token
# text. No identifier and no matching PR work product still exits 0.
#
# Environment: PAPERCLIP_API_URL, PAPERCLIP_API_KEY (required),
# PAPERCLIP_COMPANY_ID (labels, children, PR work-product lookup, standing issue), PAPERCLIP_QA_WORKFLOW
# (`qa-linux` | `qa-windows-harness`), PAPERCLIP_QA_RESULT (`success` |
# `failure` for this workflow's required jobs), PAPERCLIP_QA_JOBS (free text,
# e.g. `static=success core=success`), PAPERCLIP_HEAD_SHA (PR head, not the
# merge commit), PAPERCLIP_PR_NUMBER, GH_TOKEN (reads the other workflow's run
# and the PR labels), PAPERCLIP_QA_ARTIFACT (fallback artifact name),
# PAPERCLIP_ISSUE_IDENTIFIER / PAPERCLIP_FALLBACK_ISSUE_ID (issue override).
set -euo pipefail
export PAPERCLIP_REDACT_PY="$(cd "$(dirname "$0")" && pwd)/paperclip-redact.py"

for report in "$@"; do
  [[ -f "$report" ]] || {
    printf 'qa-report not found: %s\n' "$report" >&2
    exit 2
  }
done

api="${PAPERCLIP_API_URL:-https://slategray-dolphin-616144.hostingersite.com}"
export PAPERCLIP_API_URL="${api%/}"
[[ -n "${PAPERCLIP_API_KEY:-}" ]] || {
  printf 'PAPERCLIP_API_KEY is unset; QA gate was not posted\n' >&2
  exit 1
}

exec python3 - "$@" <<'PY'
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

_redact_path = Path(os.environ.get("PAPERCLIP_REDACT_PY") or "")
if not _redact_path.is_file():
    _redact_path = Path(os.environ.get("GITHUB_WORKSPACE") or ".") / ".github/scripts/paperclip-redact.py"
_spec = importlib.util.spec_from_file_location("paperclip_redact", _redact_path)
_redact_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_redact_mod)
redact_obj = _redact_mod.redact_obj

def api_base(url):
    """Strip a trailing slash and an optional `/api` suffix (GitHub secrets vary)."""
    url = (url or "").rstrip("/")
    if url.endswith("/api"):
        url = url[:-4]
    return url


API = api_base(os.environ["PAPERCLIP_API_URL"])
KEY = os.environ["PAPERCLIP_API_KEY"]
COMPANY = os.environ.get("PAPERCLIP_COMPANY_ID", "")
USER_AGENT = "projectag-qa-gate/1"
WORKFLOW = os.environ.get("PAPERCLIP_QA_WORKFLOW") or "qa-linux"
OWN_RESULT = (os.environ.get("PAPERCLIP_QA_RESULT") or "success").lower()
JOBS = os.environ.get("PAPERCLIP_QA_JOBS", "").strip()
HEAD = os.environ.get("PAPERCLIP_HEAD_SHA") or os.environ.get("GITHUB_SHA") or "unknown"
PR_NUMBER = os.environ.get("PAPERCLIP_PR_NUMBER", "").strip()
NIGHTLY = os.environ.get("PAPERCLIP_NIGHTLY", "") == "1"
REPO = os.environ.get("GITHUB_REPOSITORY", "fvlvte/projectag")
SERVER = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
RUN_URL = f"{SERVER}/{REPO}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
GH_TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
REF = os.environ.get("GITHUB_HEAD_REF") or os.environ.get("GITHUB_REF_NAME") or os.environ.get("GITHUB_REF") or ""
AWAITING_LABEL = "awaiting-ci"
REQUIRED_WORKFLOWS = ["qa-linux", "qa-windows-harness"]
STANDING_TITLE = "QA nightly"
FOOTER = (
    "The artifact contains `qa-report.json`, `report.md`, per-scenario state/tree/event JSON, engine logs and raw plus "
    "annotated screenshots. Software-adapter output (WARP, lavapipe) is client-harness evidence, "
    "not physical GPU, OS-input or hosted-backend acceptance; "
    "a renderer of `not reported` means the run did not record its adapter."
)


def response_class(payload):
    """Classify a Paperclip body for logs. Never include token-bearing text."""
    if isinstance(payload, dict):
        if payload.get("id") and (payload.get("identifier") or payload.get("title") is not None):
            return "json-issue"
        if payload.get("error") or payload.get("code"):
            return "json-error"
        return "json-object"
    if payload is None:
        return "empty"
    text = payload if isinstance(payload, str) else str(payload)
    stripped = text.lstrip()
    if stripped.startswith("<!") or stripped[:5].lower() == "<html":
        return "html"
    return "text"


def _parse_body(raw):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return raw.decode(errors="replace")[:400] if isinstance(raw, (bytes, bytearray)) else str(raw)[:400]


def paperclip_headers(method, has_body):
    """Match upload-paperclip-artifact.sh: no Content-Type on GET."""
    headers = {
        "Authorization": f"Bearer {KEY}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if has_body:
        headers["Content-Type"] = "application/json"
    return headers


def paperclip_urllib(method, path, body=None):
    if isinstance(body, dict):
        body = redact_obj(body)
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        API + path, data=data, method=method, headers=paperclip_headers(method, data is not None)
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, _parse_body(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, _parse_body(e.read())
    except (urllib.error.URLError, OSError) as e:
        # No HTTP response at all (DNS, connection refused, ENETUNREACH). Seen on
        # a GitHub-hosted runner where a same-run curl to the same host
        # succeeded, and it crashed the gate job instead of posting a comment.
        # paperclip() retries this with curl the way it retries a 403.
        return 597, f"urllib-transport: {e}"


def paperclip_curl(method, path, body=None):
    """Same GET as upload-paperclip-artifact.sh (curl, no Content-Type)."""
    if isinstance(body, dict):
        body = redact_obj(body)
    out = tempfile.NamedTemporaryFile(delete=False)
    out.close()
    data_path = None
    cmd = [
        "curl", "-sS",
        "-o", out.name,
        "-w", "%{http_code}",
        "-X", method,
        "-H", f"Authorization: Bearer {KEY}",
        "-H", "Accept: application/json",
        "--max-time", "60",
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
        if proc.returncode != 0 and status < 300:
            return 599, "curl-transport"
        return status, payload
    except FileNotFoundError:
        return 599, "curl-missing"
    finally:
        try:
            os.unlink(out.name)
        except OSError:
            pass
        if data_path:
            try:
                os.unlink(data_path)
            except OSError:
                pass


def paperclip(method, path, body=None):
    # GitHub-hosted runners: Python urllib GET /api/issues/{ident} returned HTTP 403
    # for AGS-43 (run 35016792043) while curl with the same board key succeeded
    # minutes later in upload-paperclip-artifact.sh. Retry 403s with curl.
    status, payload = paperclip_urllib(method, path, body)
    if status in (403, 597):
        curl_status, curl_payload = paperclip_curl(method, path, body)
        print(
            f"Paperclip {method} {path} urllib HTTP {status} class={response_class(payload)}; "
            f"retry curl HTTP {curl_status} class={response_class(curl_payload)}",
            file=sys.stderr,
        )
        return curl_status, curl_payload
    return status, payload


def github(path):
    if not GH_TOKEN:
        return None
    req = urllib.request.Request(f"https://api.github.com{path}", headers={
        "Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        print(f"GitHub {path} -> HTTP {e.code}", file=sys.stderr)
        return None


def artifact_for(path):
    m = re.search(r"(projectag-qa-[a-z0-9-]+-evidence)", path)
    if m:
        return m.group(1)
    return os.environ.get("PAPERCLIP_QA_ARTIFACT") or "projectag-qa-linux-evidence"


def summarize(path):
    report = json.load(open(path, encoding="utf-8"))
    scenarios = report.get("scenarios", [])
    passed = [s["id"] for s in scenarios if s.get("result") == "pass"]
    failed = [s["id"] for s in scenarios if s.get("result") in ("fail", "error")]
    blocked = [s["id"] for s in report.get("blocked", [])]
    for s in scenarios:
        if s.get("result") == "blocked" and s.get("id") not in blocked:
            blocked.append(s["id"])
    not_run = [s["id"] for s in report.get("not_run", [])]
    host = report.get("host", {})
    renderer = host.get("renderer") or "not reported"
    artifact = artifact_for(path)
    lines = [
        f"**{artifact}** — driver `{host.get('driver', 'unknown')}` on `{host.get('os', 'unknown')}`, renderer: `{renderer}`",
        f"- Passed: {', '.join(passed) if passed else 'none'}",
        f"- Failed/error: {', '.join(failed) if failed else 'none'}",
        f"- Blocked: {', '.join(blocked) if blocked else 'none'}",
        f"- Not run: {', '.join(not_run) if not_run else 'none'}",
        f"- Evidence artifact: `{artifact}` (`gh run download <run-id> --name {artifact}`)",
    ]
    detail_lines = []
    for s in scenarios:
        evidence = [os.path.basename(str(e)) for e in s.get("evidence") or []]
        pngs = [e for e in evidence if e.endswith(".png")]
        detail = (s.get("detail") or "").strip().replace("\n", " ")
        if len(detail) > 240:
            detail = detail[:237] + "..."
        parts = [f"`{s['id']}`: **{s.get('result')}**"]
        if detail:
            parts.append(detail)
        if pngs:
            parts.append("screenshots: " + ", ".join(pngs))
        detail_lines.append("- " + " — ".join(parts))
    for s in report.get("blocked", []):
        detail_lines.append(f"- `{s['id']}`: blocked — {s.get('reason', '')}; next: {s.get('next_action', 'see report')}")
    if detail_lines:
        lines += ["", "Per scenario:", *detail_lines]
    return "\n".join(lines), bool(failed)


def other_workflow_states():
    """Latest run per required workflow for the PR head, from the GitHub API. This run's own
    status is `in_progress` while the report job executes, so it is taken from the environment."""
    states = {}
    runs = github(f"/repos/{REPO}/actions/runs?head_sha={HEAD}&per_page=50") if HEAD != "unknown" else None
    for run in (runs or {}).get("workflow_runs", []):
        name = run.get("name")
        if name in REQUIRED_WORKFLOWS and name != WORKFLOW and name not in states:
            states[name] = {"status": run.get("status"), "conclusion": run.get("conclusion"), "url": run.get("html_url")}
    return states


def pr_view():
    if not PR_NUMBER:
        return None
    return github(f"/repos/{REPO}/pulls/{PR_NUMBER}")


def compute_gate(pr):
    """Returns (gate, required_line, next_line). Own workflow from env; qa-windows-harness only when labelled."""
    labels = {l.get("name") for l in (pr or {}).get("labels", [])}
    windows_required = "qa-windows" in labels
    others = other_workflow_states()
    parts = []
    outcomes = []
    own_jobs = f" ({JOBS})" if JOBS else ""
    parts.append(f"{WORKFLOW}={OWN_RESULT}{own_jobs} {RUN_URL}")
    outcomes.append("pass" if OWN_RESULT == "success" else "fail")
    for name in REQUIRED_WORKFLOWS:
        if name == WORKFLOW:
            continue
        if name == "qa-windows-harness" and not windows_required:
            parts.append(f"{name}=not labelled")
            continue
        state = others.get(name)
        if state is None:
            parts.append(f"{name}=pending (no run for this head yet)")
            outcomes.append("pending")
        elif state["status"] != "completed":
            parts.append(f"{name}={state['status']} {state['url']}")
            outcomes.append("pending")
        else:
            parts.append(f"{name}={state['conclusion']} {state['url']}")
            outcomes.append("pass" if state["conclusion"] == "success" else "fail")
    if "fail" in outcomes:
        gate = "fail"
    elif "pending" in outcomes:
        gate = "pending"
    else:
        gate = "pass"
    if not PR_NUMBER or pr is None:
        pr_note = "no pull request for this run"
    elif pr.get("head", {}).get("sha") != HEAD:
        pr_note = f"superseded: PR head is now {pr['head']['sha'][:12]}"
        gate = "stale"
    else:
        pr_note = f"#{PR_NUMBER} {pr.get('html_url', '')}"
    return gate, "Required: " + " · ".join(parts), pr_note


def work_product_list(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("workProducts", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def pr_work_product_matches(wp, pr_number):
    """True when a work product is the GitHub pull_request for this PR number."""
    if not isinstance(wp, dict) or not pr_number:
        return False
    if wp.get("type") != "pull_request":
        return False
    ext = str(wp.get("externalId") or "").strip().lstrip("#")
    if ext == pr_number:
        return True
    url = str(wp.get("url") or "")
    return re.search(rf"/pulls?/{re.escape(pr_number)}(?:[/?#]|$)", url) is not None


def route_active_child(issue):
    # Compatibility with children that inherited the parent's branch name: when the branch issue
    # is not the one being worked, and exactly one child is, the gate belongs to that child.
    if COMPANY and issue.get("status") not in ("in_progress", "in_review"):
        st, issues = paperclip("GET", f"/api/companies/{COMPANY}/issues")
        if st < 300 and isinstance(issues, list):
            active = [i for i in issues if i.get("parentId") == issue["id"] and i.get("status") in ("in_progress", "in_review")]
            if len(active) == 1:
                print(f"routing to child {active[0].get('identifier')} of {issue.get('identifier')}")
                return active[0]
    return issue


def resolve_issue_by_pr():
    """Issue that already has a pull_request work product for PAPERCLIP_PR_NUMBER."""
    if not PR_NUMBER or not COMPANY:
        return None
    st, issues = paperclip("GET", f"/api/companies/{COMPANY}/issues")
    if st >= 300 or not isinstance(issues, list):
        print(
            f"Paperclip issue list for PR #{PR_NUMBER} failed HTTP {st} class={response_class(issues)}",
            file=sys.stderr,
        )
        sys.exit(1)
    matches = []
    for candidate in issues:
        iid = candidate.get("id")
        if not iid:
            continue
        wst, products = paperclip("GET", f"/api/issues/{iid}/work-products")
        if wst >= 300:
            continue
        if any(pr_work_product_matches(wp, PR_NUMBER) for wp in work_product_list(products)):
            matches.append(candidate)
    if not matches:
        return None
    active = [i for i in matches if i.get("status") in ("in_progress", "in_review")]
    chosen = active if active else matches
    if len(chosen) != 1:
        print(
            f"multiple Paperclip issues own a pull_request work product for PR #{PR_NUMBER}; nothing posted",
            file=sys.stderr,
        )
        sys.exit(0)
    print(f"routing via PR #{PR_NUMBER} pull_request work product to {chosen[0].get('identifier')}")
    return chosen[0]


def resolve_issue():
    ident = os.environ.get("PAPERCLIP_ISSUE_IDENTIFIER", "")
    if not ident:
        m = re.search(r"([A-Za-z]+-[0-9]+)", REF)
        ident = m.group(1).upper() if m else os.environ.get("PAPERCLIP_FALLBACK_ISSUE_ID", "")
    if ident:
        status, issue = paperclip("GET", f"/api/issues/{ident}")
        if status >= 300 or not isinstance(issue, dict):
            print(
                f"Paperclip issue lookup for {ident} failed HTTP {status} class={response_class(issue)}",
                file=sys.stderr,
            )
            sys.exit(1)
        return route_active_child(issue)
    issue = resolve_issue_by_pr()
    if issue:
        return route_active_child(issue)
    extra = f" and no pull_request work product for PR #{PR_NUMBER}" if PR_NUMBER else ""
    print(f"no Paperclip issue identifier in ref {REF!r}{extra}; nothing posted", file=sys.stderr)
    sys.exit(0)


def label_id(name):
    if not COMPANY:
        return None
    st, labels = paperclip("GET", f"/api/companies/{COMPANY}/labels")
    if st >= 300 or not isinstance(labels, list):
        return None
    for label in labels:
        if label.get("name") == name:
            return label.get("id")
    return None


def issue_label_ids(issue):
    ids = []
    for label in issue.get("labels") or []:
        ids.append(label["id"] if isinstance(label, dict) else label)
    return ids


def standing_issue():
    st, issues = paperclip("GET", f"/api/companies/{COMPANY}/issues") if COMPANY else (500, None)
    if st < 300 and isinstance(issues, list):
        for i in issues:
            if i.get("title") == STANDING_TITLE and i.get("status") not in ("cancelled",):
                return i
    if not COMPANY:
        print("PAPERCLIP_COMPANY_ID is unset; cannot find or create the standing nightly issue", file=sys.stderr)
        sys.exit(1)
    st, agents = paperclip("GET", f"/api/companies/{COMPANY}/agents")
    analyst = next((a.get("id") for a in (agents if isinstance(agents, list) else []) if a.get("name") == "QA Analyst"), None)
    body = {
        "title": STANDING_TITLE,
        "description": "Standing issue for the 03:00 UTC `qa-linux` run on master. Every night CI appends one comment "
                       "with every job's evidence; QA Analyst triages the newest comment, opens `Nightly regression: "
                       "<scenario>` issues for new failures and leaves this issue `todo`.",
        "status": "todo",
        "priority": "medium",
    }
    if analyst:
        body["assigneeAgentId"] = analyst
    st, created = paperclip("POST", f"/api/companies/{COMPANY}/issues", body)
    if st >= 300 or not isinstance(created, dict):
        print(f"could not create the standing nightly issue HTTP {st} {created}", file=sys.stderr)
        sys.exit(1)
    return created


def main():
    reports = sys.argv[1:]
    summaries = [summarize(p) for p in reports]
    summary_text = "\n\n".join(text for text, _ in summaries) if summaries else "No `qa-report.json` was produced by this run."

    if NIGHTLY:
        issue = standing_issue()
        result = "failure" if OWN_RESULT != "success" or any(f for _, f in summaries) else "success"
        body = "\n".join([
            f"QA nightly: {result} — {WORKFLOW} {RUN_URL}",
            f"Head: `{HEAD[:12]}` (master) · Jobs: {JOBS or 'not reported'}",
            "Next: QA Analyst compares with the previous nightly comment and opens `Nightly regression: <scenario>` for new failures.",
            "", summary_text, "", FOOTER,
        ])
        st, resp = paperclip("POST", f"/api/issues/{issue['id']}/comments", {"body": body})
        if st >= 300:
            print(f"nightly comment failed HTTP {st} {resp}", file=sys.stderr)
            sys.exit(1)
        if issue.get("status") in ("done", "backlog"):
            paperclip("PATCH", f"/api/issues/{issue['id']}", {"status": "todo", "comment": "Reopened for the nightly triage."})
        print(f"posted nightly summary on {issue.get('identifier')}")
        sys.exit(0)

    issue = resolve_issue()
    pr = pr_view()
    gate, required_line, pr_note = compute_gate(pr)
    branch = REF or "unknown"
    awaiting = label_id(AWAITING_LABEL)
    has_awaiting = awaiting is not None and awaiting in issue_label_ids(issue)

    if gate == "pending":
        print(f"gate pending for {issue.get('identifier')} ({required_line}); the last required workflow posts the gate")
        sys.exit(0)

    if gate == "pass":
        if issue.get("status") == "in_progress" and has_awaiting:
            next_line = "Next: advanced to the review stages (label `awaiting-ci` removed); QA Analyst decides from this evidence."
        elif issue.get("status") == "in_review":
            next_line = "Next: QA Analyst decides from this evidence in one run."
        else:
            next_line = "Next: none required; the issue is not waiting on CI (no `awaiting-ci` label or not in progress)."
    elif gate == "fail":
        next_line = "Next: implementer answers every failing id below (product defect) or files the gate failure as a `qa` issue for Harmony Lead (infrastructure) — see the fleet docs; then pushes and re-adds `awaiting-ci`."
    else:
        next_line = "Next: none; this run is for a superseded head."

    body = "\n".join([
        f"QA gate: {gate} — {WORKFLOW} {RUN_URL}",
        f"Head: `{HEAD[:12]}` ({branch}) · PR: {pr_note}",
        required_line,
        next_line,
        "", summary_text, "", FOOTER,
    ])

    if has_awaiting and gate in ("pass", "fail"):
        remaining = [i for i in issue_label_ids(issue) if i != awaiting]
        st, resp = paperclip("PATCH", f"/api/issues/{issue['id']}", {"labelIds": remaining})
        if st >= 300:
            print(f"could not remove {AWAITING_LABEL} HTTP {st} {resp}", file=sys.stderr)

    if gate == "pass" and issue.get("status") == "in_progress" and has_awaiting:
        st, resp = paperclip("PATCH", f"/api/issues/{issue['id']}", {"status": "done", "comment": body})
        if st < 300:
            print(f"gate pass: advanced {issue.get('identifier')} to review")
            sys.exit(0)
        print(f"advance failed HTTP {st} {resp}; posting the gate as a comment instead", file=sys.stderr)

    st, resp = paperclip("POST", f"/api/issues/{issue['id']}/comments", {"body": body})
    if st >= 300:
        print(f"Paperclip QA gate comment failed HTTP {st} {resp}", file=sys.stderr)
        sys.exit(1)
    print(f"posted QA gate {gate} on {issue.get('identifier')}")


if __name__ == "__main__":
    main()
PY
