#!/usr/bin/env python3
"""Upload Windows standalone zips to Cloudflare R2 and prune old clients.

The S3 API is https://<ACCOUNT_ID>.r2.cloudflarestorage.com (region auto).
Public downloads use R2_PUBLIC_BASE_URL (custom domain or r2.dev). Each zip is
stamped with the branch and commit it was built from. Retention is per branch:
the STANDALONE_KEEP_PER_BRANCH newest zips of every live branch, the zip of
every live branch head (both from `git ls-remote --heads`), anything younger
than STANDALONE_KEEP_AGE_DAYS, plus the zip just uploaded, with R2_MAX_BYTES
(default 2_000_000_000) as a backstop that evicts live heads last. When the
live heads cannot be read the run deletes nothing, and fails if what it kept
exceeds the cap. `latest.zip` is no longer a copy: the publisher writes the
tiny pointer `latest.json` and the CDN Worker answers `latest.zip` with a
redirect to the zip it names. Cloudflare's 10 GB free tier is per ACCOUNT, so
this one prefix must never be budgeted all of it; scripts/r2-usage.py reports
and enforces the account total.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from typing import Any

from standalone_zip_crypto import require_encrypted

from r2_client import (
    DEFAULT_MAX_BYTES,
    R2Client,
    credentials_present,
    die,
    env,
    log,
    parse_max_bytes,
    parse_timestamp,
    plan_prune,
    public_object_url,
    self_test_sigv4,
    utc_now,
)

DEFAULT_PREFIX = "windows-standalone"
DEFAULT_BUCKET = "engine-windows-standalone"
DEFAULT_PUBLIC_BASE = "https://builds.projectag.online"
# A QA prefix may not claim the account's whole free tier.
STANDALONE_MAX_BYTES = 2_000_000_000
# Retention is by branch, not a global count: a busy feature branch can no
# longer evict the only zip of a quieter one. Two per branch keeps the zip a
# tester may still be downloading when the next push lands.
STANDALONE_KEEP_PER_BRANCH = 2
# Spare a per-commit zip an agent just posted to a Paperclip issue, including
# zips that predate the branch stamp. The byte cap is a backstop and wins over
# this floor, the per-branch count and the live-head rule; raise
# STANDALONE_MAX_BYTES deliberately (and inside the account budget
# scripts/r2-usage.py enforces) to widen it.
STANDALONE_KEEP_AGE_DAYS = 3
# The pointer the CDN Worker reads to redirect `latest.zip`
# (cloudflare/cdn-worker/src/redirects.js). A few hundred bytes instead of a
# second copy of the newest zip.
LATEST_POINTER = "latest.json"
# The full copy the publisher wrote before the pointer. Nothing writes it any
# more; it stays pinned until the owner deletes it once the Worker route is
# live, so the stable URL never breaks in between.
LEGACY_LATEST = "latest.zip"
# x-amz-meta-* names stamped on each zip.
BRANCH_META = "branch"
COMMIT_META = "commit"
# HEAD requests per publish to read the branch stamps. The prune keeps the
# prefix far below this; zips past it count as unstamped.
MAX_BRANCH_LOOKUPS = 200
ZIP_NAME = re.compile(r"^engine-windows-standalone-([0-9a-f]{7,40})\.zip$")
# Writing QA zips into a CDN bucket lands them under a prefix nothing prunes.
FORBIDDEN_BUCKETS = frozenset({"projectag-cdn", "projectag-inbox"})


def prefix_from_env() -> str:
    return env("R2_PREFIX", DEFAULT_PREFIX).strip("/")


def target_bucket() -> str:
    """Resolve the publish bucket the way R2Client does, and refuse the two
    buckets that are definitionally wrong for QA zips. A direnv-activated
    shell exports R2_BUCKET=projectag-cdn, and windows-standalone/ objects in
    the CDN bucket sit under a prefix no CDN prune ever lists."""
    bucket = env("R2_BUCKET") or DEFAULT_BUCKET
    if bucket in FORBIDDEN_BUCKETS:
        die(
            f"refusing to publish QA zips to {bucket}; unset R2_BUCKET or set "
            f"WINDOWS_STANDALONE_R2_BUCKET={DEFAULT_BUCKET}"
        )
    return bucket


def managed_object_name(key: str, prefix: str) -> str | None:
    expected = f"{prefix}/"
    if not key.startswith(expected):
        return None
    name = key[len(expected) :]
    if name in (LEGACY_LATEST, LATEST_POINTER):
        return name
    if name.startswith("engine-windows-standalone") and name.endswith(".zip"):
        return name
    return None


def zip_commit(key: str) -> str:
    """The commit prefix in a per-commit zip name, or "" for any other key."""
    match = ZIP_NAME.match(key.rsplit("/", 1)[-1])
    return match.group(1) if match else ""


def meta_value(text: str) -> str:
    """An x-amz-meta value: ASCII only, so anything outside a branch-name
    alphabet is percent-encoded, and bounded."""
    out = []
    for byte in text.strip().encode("utf-8"):
        char = chr(byte)
        if char.isascii() and (char.isalnum() or char in "._/-"):
            out.append(char)
        else:
            out.append(f"%{byte:02X}")
    return "".join(out)[:200]


def current_branch() -> str:
    """The branch this zip was built from: a pull request's head branch, else
    the pushed or dispatched ref name (GitHub Actions), else STANDALONE_BRANCH."""
    return (
        env("STANDALONE_BRANCH") or env("GITHUB_HEAD_REF") or env("GITHUB_REF_NAME")
    ).strip()


def parse_refs(text: str) -> dict[str, str]:
    """`git ls-remote --heads` output as {branch stamp: 12-character head}. The
    branch is spelled the way `meta_value` stamps it on a zip, so the keys
    compare directly with the stamps `zip_branches` reads back."""
    refs: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if (
            len(parts) == 2
            and parts[1].startswith("refs/heads/")
            and len(parts[1]) > len("refs/heads/")
            and re.fullmatch(r"[0-9a-f]{40}", parts[0])
        ):
            refs[meta_value(parts[1][len("refs/heads/") :])] = parts[0][:12]
    return refs


def parse_heads(text: str) -> set[str]:
    return set(parse_refs(text).values())


def live_branch_heads(cwd: str | None = None) -> set[str] | None:
    """12-character commit prefixes of every branch head on the remote, or None
    when they cannot be read (see `live_branch_refs`)."""
    refs = live_branch_refs(cwd)
    return None if refs is None else set(refs.values())


def live_branch_refs(cwd: str | None = None) -> dict[str, str] | None:
    """Every live branch on the remote as {branch stamp: 12-character head},
    or None when they cannot be read. None makes the prune keep everything.

    STANDALONE_LIVE_HEADS_FILE names a saved `git ls-remote --heads` output
    (fixtures, or an operator without network); otherwise this runs
    `git ls-remote --heads` against STANDALONE_GIT_REMOTE (default origin) in
    `cwd` (default the working directory), which the CI checkout's persisted
    token can read.
    """
    path = env("STANDALONE_LIVE_HEADS_FILE")
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            return None
    else:
        remote = env("STANDALONE_GIT_REMOTE", "origin")
        try:
            result = subprocess.run(
                ["git", "ls-remote", "--heads", remote],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                cwd=cwd,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        text = result.stdout
    # A repository always has a branch; an empty answer is not "no heads".
    return parse_refs(text) or None


def plan_prune_per_branch(
    objects: list[dict[str, Any]],
    branches: dict[str, str],
    refs: dict[str, str] | None,
    pins: list[str],
    max_bytes: int,
    keep_per_branch: int = STANDALONE_KEEP_PER_BRANCH,
    keep_age_days: int = STANDALONE_KEEP_AGE_DAYS,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Retention for one QA prefix.

    `refs` maps every live branch (its stamp spelling) to its 12-character
    head. `pins` (the zip just uploaded, the pointer, the legacy copy,
    unmanaged keys) and every key that is not a per-commit zip are never
    deleted. A zip is retained when it is the zip of a live branch head, one
    of the `keep_per_branch` newest zips stamped with a branch that is still
    live (pinned zips count toward that number), or younger than
    `keep_age_days`. A zip of a deleted branch counts only through the head
    and age rules, like an unstamped one. The byte cap then evicts retained
    zips oldest first, live heads last, and names them in `capped_keys`.
    With `refs` None nothing is deleted: a missing head list must not look
    like a repository without branches; `over_budget` still reports a kept
    total above the cap.
    """
    heads = None if refs is None else set(refs.values())
    live = set() if refs is None else set(refs)
    by_key: dict[str, dict[str, Any]] = {}
    for obj in objects:
        key = str(obj.get("Key") or "")
        if key:
            by_key[key] = {
                "Key": key,
                "Size": int(obj.get("Size") or 0),
                "LastModified": str(obj.get("LastModified") or ""),
            }
    pinned_lookup = {key for key in pins if key}
    pinned = [o for k, o in by_key.items() if k in pinned_lookup or not zip_commit(k)]
    zips = [o for k, o in by_key.items() if zip_commit(k)]
    zips.sort(key=lambda o: (o["LastModified"], o["Key"]), reverse=True)
    floor = (now or utc_now()) - timedelta(days=keep_age_days) if keep_age_days > 0 else None
    rank: dict[str, int] = {}
    reasons: dict[str, str] = {}
    retained: list[dict[str, Any]] = []
    deleted: list[dict[str, Any]] = []
    for obj in zips:
        key = obj["Key"]
        branch = branches.get(key, "")
        position = rank.get(branch, 0)
        if branch:
            rank[branch] = position + 1
        if key in pinned_lookup:
            continue
        if heads is None:
            reasons[key] = "live branch heads unknown"
        elif zip_commit(key)[:12] in heads:
            reasons[key] = "live branch head"
        elif branch in live and position < keep_per_branch:
            reasons[key] = f"newest {keep_per_branch} of {branch}"
        else:
            stamp = parse_timestamp(obj["LastModified"])
            if floor is not None and stamp is not None and stamp >= floor:
                reasons[key] = f"younger than {keep_age_days} days"
        (retained if key in reasons else deleted).append(obj)
    pinned_bytes = sum(o["Size"] for o in pinned)
    kept = list(pinned)
    used = pinned_bytes
    capped: list[str] = []
    # The cap reaches a live branch head only after every other retained zip.
    retained.sort(key=lambda o: reasons[o["Key"]] != "live branch head")
    for obj in retained:
        if heads is None or used + obj["Size"] <= max_bytes:
            kept.append(obj)
            used += obj["Size"]
        else:
            deleted.append(obj)
            capped.append(obj["Key"])
    deleted.sort(key=lambda o: (o["LastModified"], o["Key"]), reverse=True)
    return {
        "keep_keys": [o["Key"] for o in kept],
        "delete_keys": [o["Key"] for o in deleted],
        "keep_bytes": used,
        "pinned_bytes": pinned_bytes,
        "delete_bytes": sum(o["Size"] for o in deleted),
        "capped_keys": capped,
        "reasons": reasons,
        "heads_known": heads is not None,
        # Pinned overflow, or everything kept because the heads are unknown.
        "over_budget": used > max_bytes,
    }


def write_github_output(values: dict[str, str]) -> None:
    path = env("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def print_outputs(values: dict[str, str]) -> None:
    for key, value in values.items():
        print(f"{key}={value}")
    write_github_output(values)


def object_urls(key: str, latest_key: str) -> tuple[str, str, str]:
    base = env("R2_PUBLIC_BASE_URL") or DEFAULT_PUBLIC_BASE
    if base:
        return public_object_url(base, key), public_object_url(base, latest_key), "public"
    return "", "", "presign"


def pointer_document(key: str, branch: str, commit: str) -> dict[str, Any]:
    return {
        "schema": 1,
        "key": key,
        "file": key.rsplit("/", 1)[-1],
        "branch": branch,
        "commit": commit,
        "published": utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def write_latest_pointer(client: R2Client, pointer_key: str, doc: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
        json.dump(doc, handle, sort_keys=True)
        tmp = handle.name
    try:
        client.put_file(
            pointer_key,
            tmp,
            content_type="application/json",
            cache_control="no-cache",
        )
    finally:
        os.unlink(tmp)


def zip_branches(client: R2Client, listed: list[dict[str, Any]], known: dict[str, str]) -> dict[str, str]:
    """The branch stamp of every per-commit zip. A zip without one (uploaded
    before the stamp, or past MAX_BRANCH_LOOKUPS) maps to "", and a HEAD that
    fails is treated the same way rather than failing the publish."""
    branches = dict(known)
    lookups = 0
    for obj in listed:
        key = str(obj.get("Key") or "")
        if not zip_commit(key) or key in branches:
            continue
        if lookups >= MAX_BRANCH_LOOKUPS:
            branches[key] = ""
            continue
        lookups += 1
        try:
            headers = client.head_object(key) or {}
        except SystemExit:
            headers = {}
        branches[key] = (headers.get(f"x-amz-meta-{BRANCH_META}") or "").strip()
    return branches


def run_plan_prune(raw: str) -> int:
    payload = json.loads(raw or "{}")
    plan = plan_prune(
        list(payload.get("objects") or []),
        list(payload.get("keep_keys") or []),
        int(payload.get("max_bytes") or DEFAULT_MAX_BYTES),
    )
    json.dump(plan, sys.stdout)
    sys.stdout.write("\n")
    return 0


def self_test() -> None:
    self_test_sigv4()
    sample = [
        {"Key": "windows-standalone/old.zip", "Size": 4_000_000_000, "LastModified": "2026-01-01T00:00:00.000Z"},
        {"Key": "windows-standalone/mid.zip", "Size": 4_000_000_000, "LastModified": "2026-06-01T00:00:00.000Z"},
        {"Key": "windows-standalone/engine-windows-standalone-new.zip", "Size": 4_000_000_000, "LastModified": "2026-09-01T00:00:00.000Z"},
        {"Key": "windows-standalone/latest.zip", "Size": 4_000_000_000, "LastModified": "2026-09-01T00:00:00.000Z"},
    ]
    plan = plan_prune(
        sample,
        [
            "windows-standalone/latest.zip",
            "windows-standalone/engine-windows-standalone-new.zip",
        ],
        DEFAULT_MAX_BYTES,
    )
    if plan["delete_keys"] != ["windows-standalone/mid.zip", "windows-standalone/old.zip"]:
        die(f"prune self-test failed: {plan}")
    if plan["keep_bytes"] != 8_000_000_000:
        die(f"prune keep_bytes failed: {plan}")
    self_test_per_branch()
    self_test_stamps()
    self_test_forbidden_buckets()
    self_test_publish()
    log("self-test ok")


def self_test_publish() -> None:
    """publish() end to end against an in-memory bucket: the zip is stamped,
    `latest.json` names it, nothing is copied (the stub has no copy_object),
    and the prune follows the per-branch policy."""
    import io
    from contextlib import redirect_stdout

    prefix = DEFAULT_PREFIX
    store: dict[str, dict[str, Any]] = {}

    def seed(commit: str, stamp: str, branch: str) -> str:
        key = f"{prefix}/engine-windows-standalone-{commit}.zip"
        store[key] = {"Size": 100, "LastModified": stamp, "meta": {BRANCH_META: branch} if branch else {}, "body": b""}
        return key

    old_master = [seed(f"a0000000000{i}", f"2026-07-0{i + 1}T00:00:00Z", "master") for i in range(3)]
    head = seed("d00000000000", "2026-06-01T00:00:00Z", "")
    store[f"{prefix}/{LEGACY_LATEST}"] = {"Size": 100, "LastModified": "2026-07-03T00:00:00Z", "meta": {}, "body": b""}

    class Bucket:
        def __init__(self, *, default_bucket: str = "") -> None:
            self.bucket = default_bucket

        def put_file(self, key: str, path: str, **kwargs: Any) -> None:
            with open(path, "rb") as handle:
                body = handle.read()
            store[key] = {
                "Size": len(body),
                "LastModified": utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "meta": dict(kwargs.get("metadata") or {}),
                "body": body,
                "cache_control": kwargs.get("cache_control"),
            }

        def list_prefix(self, wanted: str) -> list[dict[str, Any]]:
            return [
                {"Key": key, "Size": row["Size"], "LastModified": row["LastModified"]}
                for key, row in sorted(store.items())
                if key.startswith(f"{wanted}/")
            ]

        def head_object(self, key: str) -> dict[str, str] | None:
            row = store.get(key)
            if row is None:
                return None
            return {f"x-amz-meta-{name}": value for name, value in row["meta"].items()}

        def delete_object(self, key: str) -> None:
            store.pop(key)

        def close(self) -> None:
            pass

    names = (
        "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "R2_PUBLIC_BASE_URL", "R2_PREFIX",
        "GITHUB_REF_NAME", "GITHUB_HEAD_REF", "GITHUB_SHA", "GITHUB_OUTPUT", "STANDALONE_BRANCH",
        "STANDALONE_LIVE_HEADS_FILE", "R2_MAX_BYTES",
    )
    saved_env = {name: os.environ.get(name) for name in names}
    saved = {name: globals()[name] for name in ("R2Client", "require_encrypted")}
    with tempfile.TemporaryDirectory() as tmp:
        heads = os.path.join(tmp, "heads")
        with open(heads, "w", encoding="utf-8") as handle:
            handle.write(f"{'d' + '0' * 39}\trefs/heads/codex/old-branch\n{'a00000000005' + '0' * 28}\trefs/heads/master\n")
        zip_path = os.path.join(tmp, "engine-windows-standalone-a00000000005.zip")
        with open(zip_path, "wb") as handle:
            handle.write(b"zip")
        try:
            globals()["R2Client"] = Bucket
            globals()["require_encrypted"] = lambda _path: None
            for name in names:
                os.environ.pop(name, None)
            os.environ.update(
                {
                    "R2_ACCESS_KEY_ID": "self-test",
                    "R2_SECRET_ACCESS_KEY": "self-test",
                    "R2_PUBLIC_BASE_URL": "https://builds.example.test",
                    "GITHUB_REF_NAME": "master",
                    "GITHUB_SHA": "a00000000005" + "0" * 28,
                    "STANDALONE_LIVE_HEADS_FILE": heads,
                }
            )
            with redirect_stdout(io.StringIO()) as captured:
                code = publish(zip_path, "standalone-windows-a00000000005", dry_run=False)
        finally:
            globals().update(saved)
            for name, value in saved_env.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
    current = f"{prefix}/engine-windows-standalone-a00000000005.zip"
    if code != 0 or current not in store:
        die("the self-test publish must upload the zip")
    if store[current]["meta"] != {BRANCH_META: "master", COMMIT_META: "a00000000005" + "0" * 28}:
        die(f"the zip must carry its branch and commit stamps: {store[current]['meta']}")
    pointer = json.loads(store[f"{prefix}/{LATEST_POINTER}"]["body"])
    if pointer["key"] != current or store[f"{prefix}/{LATEST_POINTER}"]["cache_control"] != "no-cache":
        die(f"latest.json must name the new zip and revalidate: {pointer}")
    if f"{prefix}/{LEGACY_LATEST}" not in store or store[f"{prefix}/{LEGACY_LATEST}"]["Size"] != 100:
        die("the legacy latest.zip copy is pinned and never rewritten")
    # master keeps the new zip plus its newest older one; the live head of
    # another branch survives although it is unstamped and old.
    if set(store) & set(old_master[:2]) or old_master[2] not in store or head not in store:
        die(f"per-branch prune self-test failed: {sorted(store)}")
    if "latest_url=https://builds.example.test/windows-standalone/latest.zip" not in captured.getvalue():
        die("the stable latest.zip URL must still be reported")


def self_test_forbidden_buckets() -> None:
    saved = os.environ.get("R2_BUCKET")
    try:
        os.environ.pop("R2_BUCKET", None)
        if target_bucket() != DEFAULT_BUCKET:
            die("default publish bucket self-test failed")
        for bucket in sorted(FORBIDDEN_BUCKETS):
            os.environ["R2_BUCKET"] = bucket
            try:
                target_bucket()
            except SystemExit:
                continue
            die(f"{bucket} must be rejected")
    finally:
        if saved is None:
            os.environ.pop("R2_BUCKET", None)
        else:
            os.environ["R2_BUCKET"] = saved


def self_test_stamps() -> None:
    if meta_value("AGS-77-baba-ostateczna-próba") != "AGS-77-baba-ostateczna-pr%C3%B3ba":
        die("branch stamps must be ASCII")
    if meta_value("fix/gate leaks") != "fix/gate%20leaks":
        die("a space in a branch stamp must be encoded")
    if zip_commit("windows-standalone/engine-windows-standalone-28ff1a1e38b0.zip") != "28ff1a1e38b0":
        die("zip_commit must read the commit prefix")
    for key in ("windows-standalone/latest.zip", "windows-standalone/latest.json", "windows-standalone/notes.zip"):
        if zip_commit(key):
            die(f"{key} is not a per-commit zip")
    heads = parse_heads(
        f"{'2' * 40}\trefs/heads/master\n{'3' * 40}\trefs/tags/v1\nnot a ref line\n"
        f"{'4' * 40}\trefs/heads/fix/a\n"
    )
    if heads != {"2" * 12, "4" * 12}:
        die(f"ls-remote parsing self-test failed: {heads}")
    refs = parse_refs(f"{'5' * 40}\trefs/heads/AGS-77-próba\n{'6' * 40}\trefs/heads/\n")
    if refs != {"AGS-77-pr%C3%B3ba": "5" * 12}:
        die(f"live branch names must use the zip stamp spelling: {refs}")
    saved = os.environ.get("STANDALONE_LIVE_HEADS_FILE")
    try:
        os.environ["STANDALONE_LIVE_HEADS_FILE"] = os.path.join(
            tempfile.gettempdir(), "projectag-no-such-heads-file"
        )
        if live_branch_heads() is not None:
            die("an unreadable heads file must mean 'unknown', never 'no branches'")
    finally:
        if saved is None:
            os.environ.pop("STANDALONE_LIVE_HEADS_FILE", None)
        else:
            os.environ["STANDALONE_LIVE_HEADS_FILE"] = saved
    doc = pointer_document("windows-standalone/engine-windows-standalone-28ff1a1e38b0.zip", "master", "28ff1a1e")
    if doc["file"] != "engine-windows-standalone-28ff1a1e38b0.zip" or doc["schema"] != 1:
        die(f"pointer document self-test failed: {doc}")


def self_test_per_branch() -> None:
    prefix = "windows-standalone"
    now = parse_timestamp("2026-10-02T12:00:00Z")

    def zip_key(commit: str) -> str:
        return f"{prefix}/engine-windows-standalone-{commit}.zip"

    def obj(key: str, stamp: str, size: int = 150_000_000) -> dict[str, Any]:
        return {"Key": key, "Size": size, "LastModified": stamp}

    current = zip_key("a00000000005")
    objects = [
        obj(f"{prefix}/{LEGACY_LATEST}", "2026-09-30T00:00:00Z"),
        obj(f"{prefix}/{LATEST_POINTER}", "2026-10-02T11:00:00Z", 200),
        obj(current, "2026-10-02T11:00:00Z"),
        obj(f"{prefix}/notes.txt", "2026-01-01T00:00:00Z", 10),
    ]
    branches = {current: "master"}
    # Four older master zips: the current one plus one more survive.
    for index in range(4):
        key = zip_key(f"a0000000000{index}")
        objects.append(obj(key, f"2026-09-0{index + 1}T00:00:00Z"))
        branches[key] = "master"
    # A quiet feature branch keeps both of its zips even though master is busy.
    for index in range(2):
        key = zip_key(f"b0000000000{index}")
        objects.append(obj(key, f"2026-08-0{index + 1}T00:00:00Z"))
        branches[key] = "AGS-12-feature"
    # Unstamped legacy zips: one is a live head, one is fresh, one is neither.
    head_zip, fresh_zip, stale_zip = zip_key("c00000000000"), zip_key("c00000000001"), zip_key("c00000000002")
    objects += [
        obj(head_zip, "2026-07-01T00:00:00Z"),
        obj(fresh_zip, "2026-10-01T00:00:00Z"),
        obj(stale_zip, "2026-07-02T00:00:00Z"),
    ]
    # A merged (deleted) branch is no longer listed: its newer zips fall back
    # to the head and age rules instead of keeping two forever.
    dead = [zip_key(f"d0000000000{index}") for index in range(2)]
    for index, key in enumerate(dead):
        objects.append(obj(key, f"2026-09-2{index}T00:00:00Z"))
        branches[key] = "AGS-9-merged"
    pins = [current, f"{prefix}/{LATEST_POINTER}", f"{prefix}/{LEGACY_LATEST}"]
    refs = {"master": "a00000000005", "AGS-12-feature": "e00000000000", "old-unstamped": "c00000000000"}
    plan = plan_prune_per_branch(objects, branches, refs, pins, 10**12, 2, 3, now=now)
    kept = set(plan["keep_keys"])
    expected_deleted = {
        zip_key("a00000000000"),
        zip_key("a00000000001"),
        zip_key("a00000000002"),
        stale_zip,
        *dead,
    }
    if set(plan["delete_keys"]) != expected_deleted:
        die(f"per-branch retention deleted {sorted(plan['delete_keys'])}")
    for key in (current, zip_key("a00000000003"), zip_key("b00000000000"), zip_key("b00000000001"),
                head_zip, fresh_zip, f"{prefix}/notes.txt", f"{prefix}/{LEGACY_LATEST}"):
        if key not in kept:
            die(f"per-branch retention must keep {key}")
    if plan["reasons"][head_zip] != "live branch head":
        die("an unstamped zip that is a live branch head must be kept for that reason")
    # Unknown heads: nothing is deleted, not even by the byte cap, but a kept
    # total above the cap is still reported so the publish step fails.
    blind = plan_prune_per_branch(objects, branches, None, pins, 1, 2, 3, now=now)
    if blind["delete_keys"] or blind["heads_known"]:
        die(f"unknown live heads must delete nothing: {blind['delete_keys']}")
    if not blind["over_budget"]:
        die("unknown live heads must not switch off the byte cap's failure")
    roomy = plan_prune_per_branch(objects, branches, None, pins, 10**12, 2, 3, now=now)
    if roomy["over_budget"]:
        die("a blind run inside the cap is not over budget")
    # The byte cap evicts the oldest retained zips and names them.
    capped = plan_prune_per_branch(objects, branches, refs, pins, 600_000_000, 2, 3, now=now)
    if current not in capped["keep_keys"] or not capped["capped_keys"]:
        die(f"the cap must keep pins and name what it evicted: {capped}")
    if capped["keep_bytes"] > 600_000_000 and not capped["over_budget"]:
        die(f"capped retention exceeded the cap: {capped}")
    # Live heads are evicted last: the old, quiet head outlives newer zips.
    if head_zip not in capped["keep_keys"]:
        die(f"the cap must reach a live branch head last: {capped['capped_keys']}")
    # Pinned bytes alone over the cap is the one case a prune cannot fix.
    over = plan_prune_per_branch([obj(current, "2026-10-02T11:00:00Z", 3_000_000_000)], {}, refs, [current], STANDALONE_MAX_BYTES)
    if not over["over_budget"] or over["delete_keys"]:
        die(f"over_budget must report pinned overflow without deleting: {over}")
    # The reported regression: merged branches with newer zips must not crowd
    # a quiet live branch's older head out of the cap.
    crowd = [obj(zip_key("f00000000000"), "2026-09-01T00:00:00Z", 180_000_000)]
    crowd_branches = {crowd[0]["Key"]: "quiet"}
    for branch in range(6):
        for index in range(2):
            key = zip_key(f"9{branch}00000000{index}")
            crowd.append(obj(key, f"2026-09-1{branch}T0{index}:00:00Z", 180_000_000))
            crowd_branches[key] = f"merged-{branch}"
    crowded = plan_prune_per_branch(
        crowd, crowd_branches, {"quiet": "f00000000000", "master": "0" * 12}, [], 2_000_000_000, 2, 3, now=now
    )
    if crowded["keep_keys"] != [crowd[0]["Key"]] or crowded["capped_keys"]:
        die(f"merged branches must not evict a live head: {crowded}")


def publish(path: str, tag: str, *, dry_run: bool) -> int:
    if not os.path.isfile(path):
        die(f"missing file: {path}")
    name = os.path.basename(path)
    prefix = prefix_from_env()
    # Before credentials and before the crypto check, so --dry-run catches a
    # misconfigured bucket too.
    target_bucket()
    commit_key = f"{prefix}/{name}"
    latest_key = f"{prefix}/{LEGACY_LATEST}"
    pointer_key = f"{prefix}/{LATEST_POINTER}"
    max_bytes = parse_max_bytes(STANDALONE_MAX_BYTES)
    public_url, latest_url, url_mode = object_urls(commit_key, latest_key)
    branch = current_branch()
    if not credentials_present():
        log("R2 credentials are unset — anonymous Cloudflare download skipped")
        print_outputs(
            {
                "url": "",
                "latest_url": "",
                "anonymous": "0",
                "mode": "skipped",
            }
        )
        return 0
    if dry_run:
        mode = "r2" if public_url else "r2-presign"
        print_outputs(
            {
                "url": public_url or "presign",
                "latest_url": latest_url or "presign",
                "anonymous": "1",
                "mode": mode,
                "key": commit_key,
                "latest_key": pointer_key,
                "branch": branch,
                "max_bytes": str(max_bytes),
            }
        )
        return 0
    require_encrypted(path)
    client = R2Client(default_bucket=DEFAULT_BUCKET)
    if client.bucket in FORBIDDEN_BUCKETS:
        client.close()
        die(f"refusing to publish QA zips to {client.bucket}")
    try:
        commit = env("GITHUB_SHA")
        stamps = {}
        if branch:
            stamps[BRANCH_META] = meta_value(branch)
        if commit:
            stamps[COMMIT_META] = meta_value(commit)
        client.put_file(
            commit_key,
            path,
            content_type="application/zip",
            cache_control="public, max-age=31536000, immutable",
            content_disposition=f'attachment; filename="{name}"',
            metadata=stamps or None,
        )
        write_latest_pointer(client, pointer_key, pointer_document(commit_key, branch, commit))
        listed = client.list_prefix(prefix)
        pins = [commit_key, latest_key, pointer_key]
        unmanaged_keys = []
        unmanaged_bytes = 0
        for obj in listed:
            if managed_object_name(obj["Key"], prefix):
                continue
            # Never auto-delete a key this script did not create; name it so the
            # operator can remove it with `r2-usage.py enforce --delete-unmanaged`.
            pins.append(obj["Key"])
            unmanaged_keys.append(obj["Key"])
            unmanaged_bytes += int(obj["Size"] or 0)
        for key in unmanaged_keys:
            log(f"warning: unmanaged key pinned under {prefix}/: {key}")
        branches = zip_branches(client, listed, {commit_key: meta_value(branch)})
        refs = live_branch_refs()
        if refs is None:
            log("warning: live branch heads are unreadable (git ls-remote); deleting nothing this run")
        plan = plan_prune_per_branch(
            listed,
            branches,
            refs,
            pins,
            max_bytes,
            STANDALONE_KEEP_PER_BRANCH,
            STANDALONE_KEEP_AGE_DAYS,
        )
        for key in plan["delete_keys"]:
            client.delete_object(key)
        log(
            f"r2 prefix {prefix}/ keep_bytes={plan['keep_bytes']} "
            f"pinned_bytes={plan['pinned_bytes']} "
            f"deleted={len(plan['delete_keys'])} keep_per_branch={STANDALONE_KEEP_PER_BRANCH} "
            f"heads_known={str(plan['heads_known']).lower()} max={max_bytes}"
        )
        for key in plan.get("capped_keys") or []:
            log(
                f"warning: {key} was inside the retention policy "
                f"({plan['reasons'].get(key, '')}); R2_MAX_BYTES evicted it"
            )
        print_outputs(
            {
                "keep_bytes": str(plan["keep_bytes"]),
                "deleted": str(len(plan["delete_keys"])),
                "unmanaged_bytes": str(unmanaged_bytes),
                "unmanaged_keys": str(len(unmanaged_keys)),
            }
        )
        if public_url:
            url = public_url
            latest = latest_url
            mode = "r2"
        else:
            url = client.presign_get(commit_key)
            latest = url
            mode = "r2-presign"
            log(f"R2_PUBLIC_BASE_URL is unset — posting a 7-day presigned GET for {tag}")
        print_outputs(
            {
                "url": url,
                "latest_url": latest,
                "anonymous": "1",
                "mode": mode,
                "key": commit_key,
            }
        )
        log(f"anonymous download {url}")
        # After the URL outputs, so QA still gets the link, but loud: a prefix
        # that cannot be pruned back under budget is a storage incident.
        if plan["over_budget"] and not plan["heads_known"]:
            die(
                f"live branch heads are unreadable, so nothing was pruned and the "
                f"prefix holds {plan['keep_bytes']} bytes > R2_MAX_BYTES {max_bytes}; "
                f"restore `git ls-remote --heads` access or run scripts/r2-usage.py enforce"
            )
        if plan["over_budget"]:
            die(
                f"kept objects still exceed R2_MAX_BYTES: pinned {plan['pinned_bytes']} "
                f"bytes (current zip + {LEGACY_LATEST} + {unmanaged_bytes} unmanaged) "
                f"> {max_bytes}; run scripts/r2-usage.py enforce"
            )
        return 0
    finally:
        client.close()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--plan-prune", action="store_true")
    parser.add_argument("file", nargs="?")
    parser.add_argument("tag", nargs="?", default="")
    args = parser.parse_args(argv)
    if args.self_test:
        self_test()
        return 0
    if args.plan_prune:
        return run_plan_prune(sys.stdin.read())
    if not args.file:
        die("missing zip path")
    return publish(args.file, args.tag, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
