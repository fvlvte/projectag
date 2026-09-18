#!/usr/bin/env python3
"""Upload Windows standalone zips to Cloudflare R2 and prune old clients.

The S3 API is https://<ACCOUNT_ID>.r2.cloudflarestorage.com (region auto).
Public downloads use R2_PUBLIC_BASE_URL (custom domain or r2.dev). Older
managed zips under the prefix are deleted until stored size is at most
R2_MAX_BYTES (default 10_000_000_000, Cloudflare's 10 GB free storage).
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
    public_object_url,
    self_test_sigv4,
)

DEFAULT_PREFIX = "windows-standalone"
DEFAULT_BUCKET = "engine-windows-standalone"
DEFAULT_PUBLIC_BASE = "https://builds.projectag.online"


def prefix_from_env() -> str:
    return env("R2_PREFIX", DEFAULT_PREFIX).strip("/")


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
    log("self-test ok")


def publish(path: str, tag: str, *, dry_run: bool) -> int:
    if not os.path.isfile(path):
        die(f"missing file: {path}")
    name = os.path.basename(path)
    prefix = prefix_from_env()
    commit_key = f"{prefix}/{name}"
    latest_key = f"{prefix}/latest.zip"
    max_bytes = parse_max_bytes()
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
        unmanaged_bytes = 0
        for obj in listed:
            if managed_object_name(obj["Key"], prefix):
                continue
            keep_keys.append(obj["Key"])
            unmanaged_bytes += int(obj["Size"] or 0)
        if unmanaged_bytes:
            log(f"warning: {unmanaged_bytes} bytes under {prefix}/ are not managed zip names")
        plan = plan_prune(listed, keep_keys, max_bytes)
        for key in plan["delete_keys"]:
            client.delete_object(key)
        log(
            f"r2 prefix {prefix}/ keep_bytes={plan['keep_bytes']} "
            f"deleted={len(plan['delete_keys'])} max={max_bytes}"
        )
        if plan["over_budget"]:
            log("warning: kept objects still exceed R2_MAX_BYTES (pinned current zip + latest.zip)")
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
