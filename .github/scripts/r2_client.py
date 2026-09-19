#!/usr/bin/env python3
"""Shared Cloudflare R2 S3 (SigV4) client for GitHub zip publish and CDN sync."""
from __future__ import annotations

import hashlib
import hmac
import http.client
import os
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any
from xml.etree import ElementTree as ET

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
# A bucket or operation outside the token's scope, from a reporter's view.
DENIED_STATUS = (401, 403, 501)
DEFAULT_ACCOUNT_ID = "d9513a4189039ef82fa87e6a03465344"
DEFAULT_REGION = "auto"
MAX_SINGLE_PUT = 5 * 1024 * 1024 * 1024
PRESIGN_SECONDS = 7 * 24 * 60 * 60
DEFAULT_MAX_BYTES = 10_000_000_000
# Cloudflare's free tier is per ACCOUNT, summed over every bucket below; no
# single bucket or prefix may be budgeted the whole of it.
ACCOUNT_MAX_BYTES = 10_000_000_000
ACCOUNT_BUCKETS = ("projectag-cdn", "engine-windows-standalone", "projectag-inbox")

# AWS docs: Authenticating Requests (AWS Signature Version 4), GET with Range.
_AWS_GET_RANGE_SIGNATURE = "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
# ListMultipartUploads signs the empty-valued `uploads` sub-resource as `uploads=`.
LIST_UPLOADS_CANONICAL_QUERY = "max-uploads=1000&prefix=dl%2F&uploads="


class R2AccessDenied(Exception):
    """The token is not scoped to this bucket or operation.

    Cloudflare answers 401 for a bucket outside an S3 User API token's scope
    and 403/501 for an operation it may not perform; all three mean the same
    thing to a reporter (HTTP 401/403/501).

    Account-wide reporting must degrade to UNMEASURED rather than report a
    smaller total, so the affected calls raise this instead of dying.
    """


def log(msg: str) -> None:
    print(msg, flush=True)


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def credentials_present() -> bool:
    return bool(env("R2_ACCESS_KEY_ID") and env("R2_SECRET_ACCESS_KEY"))


def normalize_base(url: str) -> str:
    return url.rstrip("/")


def public_object_url(base: str, key: str) -> str:
    return f"{normalize_base(base)}/{key}"


def parse_max_bytes(default: int = DEFAULT_MAX_BYTES) -> int:
    raw = env("R2_MAX_BYTES", str(default))
    try:
        value = int(raw)
    except ValueError:
        die(f"R2_MAX_BYTES must be an integer, got {raw!r}")
    if value <= 0:
        die("R2_MAX_BYTES must be positive")
    return value


def plan_prune(
    objects: list[dict[str, Any]],
    keep_keys: list[str],
    max_bytes: int,
) -> dict[str, Any]:
    """Keep newest managed objects under max_bytes. Always retain keep_keys."""
    keep_set = [k for k in keep_keys if k]
    keep_lookup = set(keep_set)
    by_key: dict[str, dict[str, Any]] = {}
    for obj in objects:
        key = str(obj.get("Key") or "")
        if not key:
            continue
        by_key[key] = {
            "Key": key,
            "Size": int(obj.get("Size") or 0),
            "LastModified": str(obj.get("LastModified") or ""),
        }
    pinned = []
    others = []
    for key, obj in by_key.items():
        if key in keep_lookup:
            pinned.append(obj)
        else:
            others.append(obj)
    others.sort(key=lambda o: (o["LastModified"], o["Key"]), reverse=True)
    kept: list[dict[str, Any]] = []
    deleted: list[dict[str, Any]] = []
    used = 0
    for obj in pinned:
        kept.append(obj)
        used += obj["Size"]
    for obj in others:
        size = obj["Size"]
        if used + size <= max_bytes:
            kept.append(obj)
            used += size
        else:
            deleted.append(obj)
    return {
        "keep_keys": [o["Key"] for o in kept],
        "delete_keys": [o["Key"] for o in deleted],
        "keep_bytes": used,
        "delete_bytes": sum(o["Size"] for o in deleted),
        "over_budget": used > max_bytes,
    }


def parse_timestamp(stamp: str) -> datetime | None:
    """Parse an S3 ISO-8601 LastModified/Initiated value, or None if unusable."""
    text = (stamp or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalize_objects(objects: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for obj in objects:
        key = str(obj.get("Key") or "")
        if not key:
            continue
        by_key[key] = {
            "Key": key,
            "Size": int(obj.get("Size") or 0),
            "LastModified": str(obj.get("LastModified") or ""),
        }
    return by_key


def plan_prune_keep_newest(
    objects: list[dict[str, Any]],
    keep_keys: list[str],
    max_bytes: int,
    keep_count: int,
    keep_age_days: int = 0,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Retain keep_keys, the `keep_count` newest others and anything younger
    than `keep_age_days`; delete the rest, then trim to `max_bytes`.

    Unlike plan_prune this converges on a small prefix instead of filling the
    budget: count and age are the primary retention rules and the byte cap is
    only a backstop. `pinned_bytes` is reported separately and `over_budget`
    means the pinned keys alone no longer fit, which no prune can fix.
    """
    keep_lookup = {key for key in keep_keys if key}
    by_key = _normalize_objects(objects)
    pinned = [obj for key, obj in by_key.items() if key in keep_lookup]
    others = [obj for key, obj in by_key.items() if key not in keep_lookup]
    others.sort(key=lambda o: (o["LastModified"], o["Key"]), reverse=True)
    floor = None
    if keep_age_days > 0:
        floor = (now or utc_now()) - timedelta(days=keep_age_days)
    retained: list[dict[str, Any]] = []
    deleted: list[dict[str, Any]] = []
    for index, obj in enumerate(others):
        stamp = parse_timestamp(obj["LastModified"])
        fresh = floor is not None and stamp is not None and stamp >= floor
        if index < keep_count or fresh:
            retained.append(obj)
        else:
            deleted.append(obj)
    pinned_bytes = sum(obj["Size"] for obj in pinned)
    kept = list(pinned)
    used = pinned_bytes
    # The cap is the backstop: anything it evicts was inside the count/age
    # policy, so name it rather than letting the retention rule look honoured.
    capped: list[str] = []
    for obj in retained:
        size = obj["Size"]
        if used + size <= max_bytes:
            kept.append(obj)
            used += size
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
        "over_budget": pinned_bytes > max_bytes,
    }


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret: str, datestamp: str, region: str, service: str) -> bytes:
    k_date = _sign(("AWS4" + secret).encode("utf-8"), datestamp)
    k_region = hmac.new(k_date, region.encode("utf-8"), hashlib.sha256).digest()
    k_service = hmac.new(k_region, service.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()


def uri_encode(value: str, *, slash: bool = False) -> str:
    safe = "/" if slash else ""
    return urllib.parse.quote(value, safe=safe)


def canonical_query(query: dict[str, str] | None) -> str:
    if not query:
        return ""
    parts = []
    for key in sorted(query):
        parts.append(f"{uri_encode(key)}={uri_encode(query[key])}")
    return "&".join(parts)


def canonical_headers_block(headers: dict[str, str]) -> tuple[str, str]:
    folded = {k.lower().strip(): " ".join(str(v).strip().split()) for k, v in headers.items()}
    names = sorted(folded)
    lines = "".join(f"{name}:{folded[name]}\n" for name in names)
    return lines, ";".join(names)


def hashed_canonical_request(
    method: str,
    canonical_uri: str,
    query: dict[str, str] | None,
    headers: dict[str, str],
    payload_hash: str,
) -> tuple[str, str, str]:
    header_block, signed = canonical_headers_block(headers)
    canonical = (
        f"{method}\n{canonical_uri}\n{canonical_query(query)}\n"
        f"{header_block}\n{signed}\n{payload_hash}"
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest(), signed, canonical


def authorization_header(
    *,
    access_key: str,
    secret: str,
    region: str,
    amz_date: str,
    datestamp: str,
    method: str,
    canonical_uri: str,
    query: dict[str, str] | None,
    headers: dict[str, str],
    payload_hash: str,
) -> str:
    digest, signed, _canonical = hashed_canonical_request(
        method, canonical_uri, query, headers, payload_hash
    )
    scope = f"{datestamp}/{region}/s3/aws4_request"
    string_to_sign = f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n{digest}"
    signature = hmac.new(
        signing_key(secret, datestamp, region, "s3"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return (
        "AWS4-HMAC-SHA256 "
        f"Credential={access_key}/{scope}, "
        f"SignedHeaders={signed}, "
        f"Signature={signature}"
    )


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def localname(tag: str) -> str:
    if tag.startswith("{") and "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def self_test_sigv4() -> None:
    access = "AKIAIOSFODNN7EXAMPLE"
    secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    amz_date = "20130524T000000Z"
    datestamp = "20130524"
    headers = {
        "host": "examplebucket.s3.amazonaws.com",
        "range": "bytes=0-9",
        "x-amz-content-sha256": EMPTY_SHA256,
        "x-amz-date": amz_date,
    }
    auth = authorization_header(
        access_key=access,
        secret=secret,
        region="us-east-1",
        amz_date=amz_date,
        datestamp=datestamp,
        method="GET",
        canonical_uri="/test.txt",
        query=None,
        headers=headers,
        payload_hash=EMPTY_SHA256,
    )
    signature = auth.rsplit("Signature=", 1)[1]
    if signature != _AWS_GET_RANGE_SIGNATURE:
        die(f"SigV4 self-test failed: got {signature}")
    uploads = canonical_query({"uploads": "", "max-uploads": "1000", "prefix": "dl/"})
    if uploads != LIST_UPLOADS_CANONICAL_QUERY:
        die(f"ListMultipartUploads canonical query failed: {uploads}")


class R2Client:
    def __init__(self, *, default_bucket: str = "", require_bucket: bool = True) -> None:
        self.account_id = env("R2_ACCOUNT_ID") or DEFAULT_ACCOUNT_ID
        self.access_key = env("R2_ACCESS_KEY_ID")
        self.secret = env("R2_SECRET_ACCESS_KEY")
        self.bucket = env("R2_BUCKET") or default_bucket
        self.region = env("R2_REGION", DEFAULT_REGION) or DEFAULT_REGION
        endpoint = env("R2_ENDPOINT")
        if endpoint:
            parsed = urllib.parse.urlparse(endpoint)
            if parsed.scheme != "https" or not parsed.netloc or parsed.path not in ("", "/"):
                die(
                    "R2_ENDPOINT must be an https origin such as "
                    "https://<ACCOUNT_ID>.r2.cloudflarestorage.com"
                )
            self.host = parsed.netloc
        else:
            self.host = f"{self.account_id}.r2.cloudflarestorage.com"
        if not self.access_key or not self.secret:
            die("R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY are unset")
        if require_bucket and not self.bucket:
            die("R2_BUCKET is unset")
        self._conn: http.client.HTTPSConnection | None = None

    def close(self) -> None:
        conn = self._conn
        self._conn = None
        if conn is None:
            return
        try:
            conn.close()
        except OSError:
            pass

    def _connect(self, timeout: int) -> http.client.HTTPSConnection:
        if self._conn is None:
            self._conn = http.client.HTTPSConnection(self.host, timeout=timeout)
        else:
            self._conn.timeout = timeout
        return self._conn

    def _path(self, key: str | None, *, root: bool = False) -> tuple[str, str]:
        if root:
            return "/", "/"
        if key:
            canonical_uri = f"/{uri_encode(self.bucket, slash=True)}/{uri_encode(key, slash=True)}"
        else:
            canonical_uri = f"/{uri_encode(self.bucket, slash=True)}"
        return canonical_uri, canonical_uri

    def request(
        self,
        method: str,
        key: str | None,
        *,
        query: dict[str, str] | None = None,
        extra_headers: dict[str, str] | None = None,
        body: Any = None,
        payload_hash: str,
        timeout: int = 1800,
        root: bool = False,
    ) -> tuple[int, bytes, dict[str, str]]:
        now = utc_now()
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")
        canonical_uri, path = self._path(key, root=root)
        headers = {
            "host": self.host,
            "x-amz-date": amz_date,
            "x-amz-content-sha256": payload_hash,
        }
        if extra_headers:
            headers.update(extra_headers)
        headers["authorization"] = authorization_header(
            access_key=self.access_key,
            secret=self.secret,
            region=self.region,
            amz_date=amz_date,
            datestamp=datestamp,
            method=method,
            canonical_uri=canonical_uri,
            query=query,
            headers=headers,
            payload_hash=payload_hash,
        )
        target = path
        qs = canonical_query(query)
        if qs:
            target = f"{path}?{qs}"
        last_error: Exception | None = None
        for attempt in range(2):
            conn = self._connect(timeout)
            try:
                conn.request(method, target, body=body, headers=headers)
                response = conn.getresponse()
                data = response.read()
                response_headers = {k.lower(): v for k, v in response.getheaders()}
                return response.status, data, response_headers
            except (http.client.HTTPException, OSError) as exc:
                last_error = exc
                self.close()
                if attempt == 0 and hasattr(body, "seek"):
                    try:
                        body.seek(0)
                    except OSError:
                        break
                elif attempt == 0 and body is not None:
                    break
        die(f"R2 {method} {key or self.bucket} failed: {last_error}")

    def put_file(
        self,
        key: str,
        path: str,
        *,
        content_type: str,
        cache_control: str,
        content_disposition: str = "",
        metadata: dict[str, str] | None = None,
        sha256: str = "",
        content_encoding: str = "",
    ) -> None:
        """PUT `path` under `key`. `sha256` and `content_encoding` describe the
        bytes actually sent: a pre-compressed file is uploaded as-is with
        `Content-Encoding: br` and R2 serves the header verbatim."""
        size = os.path.getsize(path)
        if size > MAX_SINGLE_PUT:
            die(f"{path} is {size} bytes; single PutObject max is {MAX_SINGLE_PUT}")
        payload_hash = sha256 or file_sha256(path)
        extra = {
            "content-type": content_type,
            "content-length": str(size),
            "cache-control": cache_control,
        }
        if content_encoding:
            extra["content-encoding"] = content_encoding
        if content_disposition:
            extra["content-disposition"] = content_disposition
        if metadata:
            for name, value in metadata.items():
                extra[f"x-amz-meta-{name}"] = value
        with open(path, "rb") as handle:
            status, body, _headers = self.request(
                "PUT",
                key,
                extra_headers=extra,
                body=handle,
                payload_hash=payload_hash,
            )
        if status not in (200, 201):
            hint = ""
            if status == 403:
                hint = (
                    f" — token cannot write s3://{self.bucket} on {self.host}. "
                    "Mint a Cloudflare R2 S3 User API token with Object Read & Write "
                    "on this bucket and set R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY "
                    "on fvlvte/projectag (not an account API token, not inbox/CDN-only)."
                )
            die(f"PutObject {key} HTTP {status}: {body[:500]!r}{hint}")
        log(f"uploaded s3://{self.bucket}/{key} ({size} bytes)")

    def copy_object(
        self,
        src_key: str,
        dest_key: str,
        *,
        content_type: str,
        cache_control: str,
        content_disposition: str = "",
        metadata: dict[str, str] | None = None,
    ) -> None:
        source = f"/{self.bucket}/{src_key}"
        extra = {
            "x-amz-copy-source": uri_encode(source, slash=True),
            "x-amz-metadata-directive": "REPLACE",
            "content-type": content_type,
            "cache-control": cache_control,
        }
        if content_disposition:
            extra["content-disposition"] = content_disposition
        if metadata:
            for name, value in metadata.items():
                extra[f"x-amz-meta-{name}"] = value
        status, body, _headers = self.request(
            "PUT",
            dest_key,
            extra_headers=extra,
            payload_hash=EMPTY_SHA256,
        )
        if status not in (200, 201):
            die(f"CopyObject {src_key} -> {dest_key} HTTP {status}: {body[:500]!r}")
        log(f"copied s3://{self.bucket}/{src_key} -> {dest_key}")

    def head_object(self, key: str) -> dict[str, str] | None:
        status, body, headers = self.request(
            "HEAD",
            key,
            payload_hash=EMPTY_SHA256,
            timeout=60,
        )
        if status == 404:
            return None
        if status != 200:
            die(f"HeadObject {key} HTTP {status}: {body[:500]!r}")
        return headers

    def object_sha256(self, key: str) -> str:
        sha, _cache_control = self.object_meta(key)
        return sha

    def object_meta(self, key: str) -> tuple[str, str]:
        headers = self.head_object(key)
        if not headers:
            return "", ""
        return (
            (headers.get("x-amz-meta-sha256") or "").strip(),
            (headers.get("cache-control") or "").strip(),
        )

    def list_buckets(self) -> list[dict[str, str]]:
        """Every bucket the credentials can see. Raises R2AccessDenied for a
        bucket-scoped token, which is the normal case for this repo's keys."""
        status, body, _headers = self.request(
            "GET",
            None,
            payload_hash=EMPTY_SHA256,
            timeout=120,
            root=True,
        )
        if status in DENIED_STATUS:
            raise R2AccessDenied(f"ListBuckets HTTP {status}")
        if status != 200:
            die(f"ListBuckets HTTP {status}: {body[:500]!r}")
        buckets: list[dict[str, str]] = []
        for node in ET.fromstring(body).iter():
            if localname(node.tag) != "Bucket":
                continue
            row = {"Name": "", "CreationDate": ""}
            for field in node:
                name = localname(field.tag)
                if name in row:
                    row[name] = field.text or ""
            if row["Name"]:
                buckets.append(row)
        return buckets

    def list_multipart_uploads(self, prefix: str = "") -> list[dict[str, Any]]:
        """Incomplete multipart uploads. ListObjectsV2 never shows their parts,
        but R2 bills them, so an account report has to ask separately."""
        uploads: list[dict[str, Any]] = []
        key_marker = ""
        upload_marker = ""
        while True:
            query = {"uploads": "", "max-uploads": "1000"}
            if prefix:
                query["prefix"] = prefix
            if key_marker:
                query["key-marker"] = key_marker
            if upload_marker:
                query["upload-id-marker"] = upload_marker
            status, body, _headers = self.request(
                "GET",
                None,
                query=query,
                payload_hash=EMPTY_SHA256,
                timeout=120,
            )
            if status in DENIED_STATUS:
                raise R2AccessDenied(f"ListMultipartUploads HTTP {status}")
            if status != 200:
                die(f"ListMultipartUploads HTTP {status}: {body[:500]!r}")
            root = ET.fromstring(body)
            truncated = ""
            next_key = ""
            next_upload = ""
            for child in root:
                name = localname(child.tag)
                if name == "Upload":
                    row: dict[str, Any] = {"Key": "", "UploadId": "", "Initiated": ""}
                    for field in child:
                        field_name = localname(field.tag)
                        if field_name in row:
                            row[field_name] = field.text or ""
                    if row["Key"] and row["UploadId"]:
                        uploads.append(row)
                elif name == "IsTruncated":
                    truncated = (child.text or "").lower()
                elif name == "NextKeyMarker":
                    next_key = child.text or ""
                elif name == "NextUploadIdMarker":
                    next_upload = child.text or ""
            if truncated == "true" and (next_key or next_upload):
                key_marker = next_key
                upload_marker = next_upload
                continue
            break
        return uploads

    def list_parts(self, key: str, upload_id: str) -> list[dict[str, Any]]:
        """The uploaded parts of one incomplete upload; their sizes are the
        billed bytes of that upload."""
        parts: list[dict[str, Any]] = []
        marker = ""
        while True:
            query = {"uploadId": upload_id, "max-parts": "1000"}
            if marker:
                query["part-number-marker"] = marker
            status, body, _headers = self.request(
                "GET",
                key,
                query=query,
                payload_hash=EMPTY_SHA256,
                timeout=120,
            )
            if status in DENIED_STATUS:
                raise R2AccessDenied(f"ListParts HTTP {status}")
            if status == 404:
                return parts
            if status != 200:
                die(f"ListParts {key} HTTP {status}: {body[:500]!r}")
            root = ET.fromstring(body)
            truncated = ""
            next_marker = ""
            for child in root:
                name = localname(child.tag)
                if name == "Part":
                    part = {"PartNumber": 0, "Size": 0}
                    for field in child:
                        field_name = localname(field.tag)
                        if field_name in part:
                            part[field_name] = int(field.text or "0")
                    parts.append(part)
                elif name == "IsTruncated":
                    truncated = (child.text or "").lower()
                elif name == "NextPartNumberMarker":
                    next_marker = child.text or ""
            if truncated == "true" and next_marker:
                marker = next_marker
                continue
            break
        return parts

    def abort_multipart_upload(self, key: str, upload_id: str) -> None:
        status, body, _headers = self.request(
            "DELETE",
            key,
            query={"uploadId": upload_id},
            payload_hash=EMPTY_SHA256,
            timeout=120,
        )
        if status not in (200, 204, 404):
            die(f"AbortMultipartUpload {key} HTTP {status}: {body[:500]!r}")
        log(f"aborted multipart s3://{self.bucket}/{key} ({upload_id})")

    def usage(self, prefix: str = "") -> dict[str, Any]:
        """Object count and stored bytes under `prefix` (empty = whole bucket)."""
        objects = self.list_prefix(prefix, as_directory=False, raise_forbidden=True)
        return {
            "objects": len(objects),
            "bytes": sum(int(obj["Size"] or 0) for obj in objects),
        }

    def list_prefix(
        self,
        prefix: str,
        *,
        as_directory: bool = True,
        raise_forbidden: bool = False,
    ) -> list[dict[str, Any]]:
        objects: list[dict[str, Any]] = []
        token = ""
        listed = prefix
        if as_directory and prefix and not prefix.endswith("/"):
            listed = f"{prefix}/"
        while True:
            query = {"list-type": "2", "prefix": listed}
            if token:
                query["continuation-token"] = token
            status, body, _headers = self.request(
                "GET",
                None,
                query=query,
                payload_hash=EMPTY_SHA256,
                timeout=120,
            )
            if raise_forbidden and status in DENIED_STATUS:
                raise R2AccessDenied(f"ListObjectsV2 HTTP {status}")
            if status != 200:
                die(f"ListObjectsV2 HTTP {status}: {body[:500]!r}")
            root = ET.fromstring(body)
            for child in root:
                if localname(child.tag) != "Contents":
                    continue
                item: dict[str, Any] = {"Key": "", "Size": 0, "LastModified": ""}
                for field in child:
                    name = localname(field.tag)
                    if name == "Key":
                        item["Key"] = field.text or ""
                    elif name == "Size":
                        item["Size"] = int(field.text or "0")
                    elif name == "LastModified":
                        item["LastModified"] = field.text or ""
                if item["Key"]:
                    objects.append(item)
            truncated = ""
            next_token = ""
            for child in root:
                name = localname(child.tag)
                if name == "IsTruncated":
                    truncated = (child.text or "").lower()
                elif name == "NextContinuationToken":
                    next_token = child.text or ""
            if truncated == "true" and next_token:
                token = next_token
                continue
            break
        return objects

    def list_prefixes(self, prefixes: list[str]) -> list[dict[str, Any]]:
        by_key: dict[str, dict[str, Any]] = {}
        for prefix in prefixes:
            as_directory = prefix.endswith("/")
            for obj in self.list_prefix(prefix, as_directory=as_directory):
                by_key[obj["Key"]] = obj
        return list(by_key.values())

    def get_file(self, key: str, dest: str, *, max_bytes: int) -> int:
        headers = self.head_object(key)
        if not headers:
            die(f"missing s3://{self.bucket}/{key}")
        raw_len = headers.get("content-length") or "0"
        try:
            size = int(raw_len)
        except ValueError:
            die(f"bad Content-Length for {key}: {raw_len!r}")
        if size > max_bytes:
            die(f"{key} is {size} bytes; max is {max_bytes}")
        status, body, _headers = self.request("GET", key, payload_hash=EMPTY_SHA256)
        if status != 200:
            die(f"GetObject {key} HTTP {status}: {body[:500]!r}")
        if len(body) > max_bytes:
            die(f"{key} body is {len(body)} bytes; max is {max_bytes}")
        parent = os.path.dirname(dest)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(dest, "wb") as handle:
            handle.write(body)
        log(f"downloaded s3://{self.bucket}/{key} ({len(body)} bytes)")
        return len(body)

    def delete_object(self, key: str) -> None:
        status, body, _headers = self.request(
            "DELETE",
            key,
            payload_hash=EMPTY_SHA256,
            timeout=120,
        )
        if status not in (200, 204):
            die(f"DeleteObject {key} HTTP {status}: {body[:500]!r}")
        log(f"deleted s3://{self.bucket}/{key}")

    def presign_get(self, key: str, expires: int = PRESIGN_SECONDS) -> str:
        now = utc_now()
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")
        canonical_uri, path = self._path(key)
        scope = f"{datestamp}/{self.region}/s3/aws4_request"
        query = {
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-Credential": f"{self.access_key}/{scope}",
            "X-Amz-Date": amz_date,
            "X-Amz-Expires": str(expires),
            "X-Amz-SignedHeaders": "host",
        }
        headers = {"host": self.host}
        digest, _signed, _canonical = hashed_canonical_request(
            "GET", canonical_uri, query, headers, "UNSIGNED-PAYLOAD"
        )
        string_to_sign = f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n{digest}"
        signature = hmac.new(
            signing_key(self.secret, datestamp, self.region, "s3"),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        query["X-Amz-Signature"] = signature
        return f"https://{self.host}{path}?{canonical_query(query)}"
