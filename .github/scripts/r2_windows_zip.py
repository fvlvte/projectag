#!/usr/bin/env python3
"""Upload Windows standalone zips to Cloudflare R2 and prune old clients.

The S3 API is https://<ACCOUNT_ID>.r2.cloudflarestorage.com (region auto).
Public downloads use R2_PUBLIC_BASE_URL (custom domain or r2.dev). Retention
is latest.zip plus the STANDALONE_KEEP newest managed zips plus anything
younger than STANDALONE_KEEP_AGE_DAYS, with R2_MAX_BYTES (default
2_000_000_000) as a backstop. Cloudflare's 10 GB free tier is per ACCOUNT, so
this one prefix must never be budgeted all of it; scripts/r2-usage.py reports
and enforces the account total.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
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
    plan_prune,
    plan_prune_keep_newest,
    public_object_url,
    self_test_sigv4,
    utc_now,
)

DEFAULT_PREFIX = "windows-standalone"
DEFAULT_BUCKET = "engine-windows-standalone"
DEFAULT_PUBLIC_BASE = "https://builds.projectag.online"
# A QA prefix may not claim the account's whole free tier.
STANDALONE_MAX_BYTES = 2_000_000_000
# Retention is primarily by count: a byte cap alone always converges on the cap.
STANDALONE_KEEP = 8
# Spare a per-commit zip an agent just posted to a Paperclip issue.
# The byte cap is a backstop and wins over this floor, so a busy day of
# pushes can still evict a zip younger than this. STANDALONE_KEEP is the
# guarantee QA can rely on; raise STANDALONE_MAX_BYTES deliberately (and
# inside the account budget scripts/r2-usage.py enforces) to widen it.
STANDALONE_KEEP_AGE_DAYS = 3
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
    if name == "latest.zip":
        return name
    if name.startswith("engine-windows-standalone") and name.endswith(".zip"):
        return name
    return None


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
    self_test_keep_newest()
    self_test_forbidden_buckets()
    log("self-test ok")


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


def self_test_keep_newest() -> None:
    latest = "windows-standalone/latest.zip"
    current = "windows-standalone/engine-windows-standalone-0000000000ff.zip"
    objects = [
        {"Key": latest, "Size": 150_000_000, "LastModified": "2026-09-19T00:00:00.000Z"},
        {"Key": current, "Size": 150_000_000, "LastModified": "2026-09-19T00:00:00.000Z"},
    ]
    for index in range(20):
        objects.append(
            {
                "Key": f"windows-standalone/engine-windows-standalone-{index:012x}.zip",
                "Size": 150_000_000,
                # 2024, so every one of them is far outside the age floor.
                "LastModified": f"2024-01-{index + 1:02d}T00:00:00.000Z",
            }
        )
    plan = plan_prune_keep_newest(
        objects,
        [latest, current],
        STANDALONE_MAX_BYTES,
        STANDALONE_KEEP,
        STANDALONE_KEEP_AGE_DAYS,
    )
    # 2 pinned + the 8 newest of the 20 unpinned ones, not "fill 2 GB".
    if len(plan["keep_keys"]) != 2 + STANDALONE_KEEP:
        die(f"keep-newest retention failed: {len(plan['keep_keys'])} kept")
    if latest in plan["delete_keys"] or current in plan["delete_keys"]:
        die("keep-newest must never delete latest.zip or the current zip")
    if plan["pinned_bytes"] != 300_000_000:
        die(f"keep-newest pinned_bytes failed: {plan['pinned_bytes']}")
    if plan["keep_bytes"] != 1_500_000_000 or plan["over_budget"]:
        die(f"keep-newest byte accounting failed: {plan}")
    if len(plan["delete_keys"]) != 12:
        die(f"keep-newest delete count failed: {plan['delete_keys']}")

    # The age floor overrides the count floor for a zip QA may still be fetching.
    fresh = [
        {
            "Key": f"windows-standalone/engine-windows-standalone-fresh{index}.zip",
            "Size": 1_000,
            "LastModified": utc_now().isoformat().replace("+00:00", "Z"),
        }
        for index in range(12)
    ]
    plan = plan_prune_keep_newest(fresh, [], STANDALONE_MAX_BYTES, 2, STANDALONE_KEEP_AGE_DAYS)
    if plan["delete_keys"]:
        die(f"age floor must retain fresh zips: {plan['delete_keys']}")

    # Pinned bytes alone over the cap is the one case a prune cannot fix.
    plan = plan_prune_keep_newest(
        [{"Key": latest, "Size": 3_000_000_000, "LastModified": "2026-09-19T00:00:00.000Z"}],
        [latest],
        STANDALONE_MAX_BYTES,
        STANDALONE_KEEP,
    )
    if not plan["over_budget"] or plan["delete_keys"]:
        die(f"over_budget must report pinned overflow without deleting: {plan}")


def publish(path: str, tag: str, *, dry_run: bool) -> int:
    if not os.path.isfile(path):
        die(f"missing file: {path}")
    name = os.path.basename(path)
    prefix = prefix_from_env()
    # Before credentials and before the crypto check, so --dry-run catches a
    # misconfigured bucket too.
    target_bucket()
    commit_key = f"{prefix}/{name}"
    latest_key = f"{prefix}/latest.zip"
    max_bytes = parse_max_bytes(STANDALONE_MAX_BYTES)
    public_url, latest_url, url_mode = object_urls(commit_key, latest_key)
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
                "latest_key": latest_key,
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
        client.put_file(
            commit_key,
            path,
            content_type="application/zip",
            cache_control="public, max-age=31536000, immutable",
            content_disposition=f'attachment; filename="{name}"',
        )
        client.copy_object(
            commit_key,
            latest_key,
            content_type="application/zip",
            cache_control="public, max-age=60",
            content_disposition='attachment; filename="engine-windows-standalone.zip"',
        )
        listed = client.list_prefix(prefix)
        keep_keys = [commit_key, latest_key]
        unmanaged_keys = []
        unmanaged_bytes = 0
        for obj in listed:
            if managed_object_name(obj["Key"], prefix):
                continue
            # Never auto-delete a key this script did not create; name it so the
            # operator can remove it with `r2-usage.py enforce --delete-unmanaged`.
            keep_keys.append(obj["Key"])
            unmanaged_keys.append(obj["Key"])
            unmanaged_bytes += int(obj["Size"] or 0)
        for key in unmanaged_keys:
            log(f"warning: unmanaged key pinned under {prefix}/: {key}")
        plan = plan_prune_keep_newest(
            listed,
            keep_keys,
            max_bytes,
            STANDALONE_KEEP,
            STANDALONE_KEEP_AGE_DAYS,
        )
        for key in plan["delete_keys"]:
            client.delete_object(key)
        log(
            f"r2 prefix {prefix}/ keep_bytes={plan['keep_bytes']} "
            f"pinned_bytes={plan['pinned_bytes']} "
            f"deleted={len(plan['delete_keys'])} keep_newest={STANDALONE_KEEP} "
            f"max={max_bytes}"
        )
        for key in plan.get("capped_keys") or []:
            log(f"warning: {key} was inside the retention policy; R2_MAX_BYTES evicted it")
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
            latest = client.presign_get(latest_key)
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
        if plan["over_budget"]:
            die(
                f"kept objects still exceed R2_MAX_BYTES: pinned {plan['pinned_bytes']} "
                f"bytes (current zip + latest.zip + {unmanaged_bytes} unmanaged) "
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
