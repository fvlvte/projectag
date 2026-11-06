#!/usr/bin/env python3
"""Turn the periodic QA sweep's shard reports into one result that reaches someone.

    qa-periodic-report.py REPORTS_DIR SUMMARY_DIR [--issue]

REPORTS_DIR holds one directory per shard, each with its qa-report.json. The
expected shards come from QA_PERIODIC_SHARDS ("id=result id=result", the job
results of the workflow). A shard whose job was skipped is recorded as not run,
which is what a shard on the owner's GPU runner does while that runner is off;
any other shard without a report failed.

Writes SUMMARY_DIR/summary.json, which `cargo agx ci status` reads, and the job
summary. Exits 1 when a scenario failed or a shard has no report.

With --issue it also keeps one open issue while the sweep is red and closes it
when the sweep is green, so a red sweep notifies the repository's owner without
blocking anything. The repository is public: the issue names shards, counts and
the run, never a scenario, its detail or a log.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ISSUE_TITLE = "QA periodic sweep is red"


def shard_results(text: str) -> dict[str, str]:
    results = {}
    for part in text.split():
        name, _, result = part.partition("=")
        if not name or not result:
            raise ValueError(f"bad QA_PERIODIC_SHARDS entry {part!r}")
        results[name] = result
    return results


def read_report(path: Path) -> dict | None:
    try:
        report = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return report if isinstance(report.get("scenarios"), list) else None


def build_summary(reports_dir: Path, results: dict[str, str], run_url: str, sha: str) -> dict:
    summary = {
        "schema_version": 1,
        "run_url": run_url,
        "sha": sha,
        "failed": [],
        "missing_shards": [],
        "skipped_shards": [],
        "shards": {},
    }
    for shard, result in sorted(results.items()):
        report = read_report(reports_dir / shard / "qa-report.json")
        if report is None:
            if result == "skipped":
                summary["skipped_shards"].append(shard)
            else:
                summary["missing_shards"].append(shard)
            summary["shards"][shard] = {"job": result, "scenarios": 0, "failed": 0, "review_pending": 0, "minutes": 0}
            continue
        failed = []
        pending = 0
        minutes = 0.0
        for row in report["scenarios"]:
            minutes += row.get("duration_ms", 0) / 60000
            outcome = row.get("result")
            review = row.get("visual_review") or {}
            if outcome == "pass":
                continue
            if outcome == "blocked" and review.get("automated_result") == "pass" and review.get("requests"):
                pending += 1
                continue
            failed.append(row.get("id", "?"))
        for row in report.get("not_run", []):
            failed.append(row.get("id", "?"))
        summary["failed"] += failed
        summary["shards"][shard] = {
            "job": result,
            "scenarios": len(report["scenarios"]),
            "failed": len(failed),
            "review_pending": pending,
            "minutes": round(minutes, 1),
        }
    summary["failed"] = sorted(set(summary["failed"]))
    return summary


def is_red(summary: dict) -> bool:
    return bool(summary["failed"] or summary["missing_shards"])


def job_summary(summary: dict) -> str:
    lines = ["## QA periodic sweep", "", "| shard | job | scenarios | failed | visual review pending | minutes |", "|---|---|---|---|---|---|"]
    for shard, row in summary["shards"].items():
        lines.append(
            f"| {shard} | {row['job']} | {row['scenarios']} | {row['failed']} | {row['review_pending']} | {row['minutes']} |"
        )
    lines += ["", f"Failing scenarios: {len(summary['failed'])}."]
    if summary["missing_shards"]:
        lines.append("No report from: " + ", ".join(summary["missing_shards"]) + ".")
    if summary["skipped_shards"]:
        lines.append("Not run, runner off: " + ", ".join(summary["skipped_shards"]) + ".")
    return "\n".join(lines) + "\n"


def issue_text(summary: dict) -> str:
    """Public text: shards, counts and the run. No scenario ids or details."""
    bad = [f"{shard}: {row['failed']} failed" for shard, row in summary["shards"].items() if row["failed"]]
    bad += [f"{shard}: no report" for shard in summary["missing_shards"]]
    return (
        f"The periodic QA sweep is red on {summary['sha'][:9]}.\n\n"
        + "\n".join(f"- {line}" for line in bad)
        + f"\n\nRun: {summary['run_url']}\n"
        + "`cargo agx ci status` names the failing scenarios. This does not block a deploy. "
        + "Policy: docs/development/testing.md.\n"
    )


def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def open_issue_number(repo: str) -> str | None:
    listed = json.loads(gh("issue", "list", "-R", repo, "--state", "open", "--limit", "100", "--json", "number,title"))
    for issue in listed:
        if issue["title"] == ISSUE_TITLE:
            return str(issue["number"])
    return None


def update_issue(repo: str, summary: dict) -> None:
    number = open_issue_number(repo)
    if is_red(summary):
        if number is None:
            gh("issue", "create", "-R", repo, "--title", ISSUE_TITLE, "--body", issue_text(summary))
        else:
            gh("issue", "comment", number, "-R", repo, "--body", issue_text(summary))
    elif number is not None:
        gh("issue", "close", number, "-R", repo, "--comment", f"Green again: {summary['run_url']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("reports", type=Path)
    parser.add_argument("summary", type=Path)
    parser.add_argument("--issue", action="store_true")
    args = parser.parse_args()
    results = shard_results(os.environ.get("QA_PERIODIC_SHARDS", ""))
    if not results:
        print("QA_PERIODIC_SHARDS is empty", file=sys.stderr)
        return 2
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_url = f"{server}/{repo}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
    summary = build_summary(args.reports, results, run_url, os.environ.get("GITHUB_SHA", ""))
    args.summary.mkdir(parents=True, exist_ok=True)
    (args.summary / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    text = job_summary(summary)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as out:
            out.write(text)
    if args.issue:
        # A failed issue update must not hide the sweep's result.
        try:
            update_issue(repo, summary)
        except (subprocess.CalledProcessError, json.JSONDecodeError, OSError) as err:
            print(f"::warning::could not update the sweep issue: {err}", file=sys.stderr)
    return 1 if is_red(summary) else 0


if __name__ == "__main__":
    sys.exit(main())
