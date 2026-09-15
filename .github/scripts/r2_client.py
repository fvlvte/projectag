#!/usr/bin/env python3
"""Shared Cloudflare R2 S3 (SigV4) client for GitHub zip publish and CDN sync."""
from __future__ import annotations

import hashlib
import hmac
import http.client
import os
import sys
import urllib.parse
from datetime import datetime, timezone
from typing import Any
from xml.etree import ElementTree as ET

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
DEFAULT_ACCOUNT_ID = "d9513a4189039ef82fa87e6a03465344"
DEFAULT_REGION = "auto"
MAX_SINGLE_PUT = 5 * 1024 * 1024 * 1024
PRESIGN_SECONDS = 7 * 24 * 60 * 60
DEFAULT_MAX_BYTES = 10_000_000_000

# AWS docs: Authenticating Requests (AWS Signature Version 4), GET with Range.
_AWS_GET_RANGE_SIGNATURE = "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"


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


class R2Client:
    def __init__(self, *, default_bucket: str = "") -> None:
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
        if not self.bucket:
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

    def _path(self, key: str | None) -> tuple[str, str]:
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
    ) -> tuple[int, bytes, dict[str, str]]:
        now = utc_now()
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")
        canonical_uri, path = self._path(key)
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
    ) -> None:
        size = os.path.getsize(path)
        if size > MAX_SINGLE_PUT:
            die(f"{path} is {size} bytes; single PutObject max is {MAX_SINGLE_PUT}")
        payload_hash = sha256 or file_sha256(path)
        extra = {
            "content-type": content_type,
            "content-length": str(size),
            "cache-control": cache_control,
        }
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
            die(f"PutObject {key} HTTP {status}: {body[:500]!r}")
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

    def list_prefix(self, prefix: str, *, as_directory: bool = True) -> list[dict[str, Any]]:
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
