"""The one place that talks S3 to RustFS.

Routers and services go through this module. Keeping the client behind a
function is also what makes storage fixable in tests.
"""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from urllib.parse import quote

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from app.config import get_settings

# Bytes per chunk when streaming an object body to the client.
_STREAM_CHUNK = 64 * 1024

# Error codes RustFS returns when the key (or the bucket) does not exist.
_MISSING_CODES = {"NoSuchKey", "NoSuchBucket", "404"}

# The most keys one DeleteObjects call may name, per the S3 API.
_DELETE_BATCH = 1000


class ObjectNotFound(LookupError):
    """The requested bucket/key pair does not exist in RustFS."""

    def __init__(self, bucket: str, key: str) -> None:
        self.bucket = bucket
        self.key = key
        super().__init__(f"no object at {bucket}/{key}")


@dataclass(frozen=True)
class ObjectStream:
    """An open object body plus the metadata needed to serve it."""

    content_type: str
    content_length: int | None
    chunks: Iterator[bytes]


@dataclass(frozen=True)
class ObjectSummary:
    """One key returned by a listing."""

    key: str
    size: int


@lru_cache
def get_client() -> Any:
    """Return the shared, thread-safe boto3 S3 client for RustFS."""
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=settings.rustfs_endpoint_url,
        aws_access_key_id=settings.rustfs_access_key,
        aws_secret_access_key=settings.rustfs_secret_key,
        region_name=settings.rustfs_region,
        # RustFS keys are path-style; virtual-host addressing would turn the
        # bucket into a subdomain that does not resolve.
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def _as_missing(exc: ClientError, bucket: str, key: str) -> ObjectNotFound | None:
    """Translate a ClientError into ObjectNotFound, or None if it is something else."""
    code = str(exc.response.get("Error", {}).get("Code", ""))
    return ObjectNotFound(bucket, key) if code in _MISSING_CODES else None


def list_objects(bucket: str, prefix: str) -> list[ObjectSummary]:
    """The objects sitting directly under ``prefix``, subfolders excluded.

    The delimiter is what keeps this cheap: a parse directory holds a handful
    of text artifacts next to an ``images/`` folder of a hundred page crops,
    and only the former come back.
    """
    try:
        response = get_client().list_objects_v2(
            Bucket=bucket, Prefix=prefix, Delimiter="/"
        )
    except ClientError as exc:
        if missing := _as_missing(exc, bucket, prefix):
            raise missing from exc
        raise

    return [
        ObjectSummary(key=o["Key"], size=o["Size"]) for o in response.get("Contents", [])
    ]


def read_object(bucket: str, key: str, *, max_bytes: int) -> tuple[bytes, bool]:
    """Read a whole object into memory, refusing anything past ``max_bytes``.

    Returns the bytes and whether the object was rejected for being too large.
    An oversized object comes back empty rather than truncated: half a JSON
    file is not something to hand on as if it were the file.
    """
    try:
        response = get_client().get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if missing := _as_missing(exc, bucket, key):
            raise missing from exc
        raise

    with response["Body"] as body:
        if (response.get("ContentLength") or 0) > max_bytes:
            return b"", True
        return body.read(), False


def open_object(bucket: str, key: str) -> ObjectStream:
    """Open an object for streaming.

    Raises ObjectNotFound if the key is absent, so a missing blob surfaces as a
    404 rather than a 500.
    """
    try:
        response = get_client().get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if missing := _as_missing(exc, bucket, key):
            raise missing from exc
        raise

    body = response["Body"]

    def chunks() -> Iterator[bytes]:
        with body:
            yield from body.iter_chunks(_STREAM_CHUNK)

    return ObjectStream(
        content_type=response.get("ContentType") or "application/octet-stream",
        content_length=response.get("ContentLength"),
        chunks=chunks(),
    )


def list_all_objects(bucket: str, prefix: str) -> list[ObjectSummary]:
    """Every object under ``prefix``, subfolders included, across all pages.

    The recursive counterpart of ``list_objects``. It is meant for one run
    directory — a few text files and a folder of page crops — not for walking
    a bucket; the caller is expected to hand it a prefix that ends in ``/``.
    """
    paginator = get_client().get_paginator("list_objects_v2")
    found: list[ObjectSummary] = []
    try:
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            found.extend(
                ObjectSummary(key=o["Key"], size=o["Size"])
                for o in page.get("Contents", [])
            )
    except ClientError as exc:
        if missing := _as_missing(exc, bucket, prefix):
            raise missing from exc
        raise
    return found


def delete_objects(bucket: str, keys: Iterable[str]) -> int:
    """Delete the named keys, in batches of at most 1000. Returns how many.

    A key that no longer exists is not an error — deleting is idempotent — but
    any key the service refuses to delete is raised, since leaving half a run
    directory behind next to a fresh one is worse than stopping.
    """
    keys = list(keys)
    client = get_client()
    for start in range(0, len(keys), _DELETE_BATCH):
        batch = keys[start : start + _DELETE_BATCH]
        response = client.delete_objects(
            Bucket=bucket,
            Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
        )
        errors = response.get("Errors") or []
        if errors:
            first = errors[0]
            raise RuntimeError(
                f"{len(errors)} object(s) could not be deleted from {bucket}; "
                f"first: {first.get('Key')}: {first.get('Message')}"
            )
    return len(keys)


def put_object(bucket: str, key: str, body: bytes, *, content_type: str) -> None:
    """Write one object in full."""
    get_client().put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)


# How long a presigned link stays valid. Long enough to read a document,
# short enough that a leaked link goes stale the same day.
PRESIGN_EXPIRES_SECONDS = 60 * 60


def presign_get(
    bucket: str,
    key: str,
    *,
    filename: str | None = None,
    expires_in: int = PRESIGN_EXPIRES_SECONDS,
) -> str:
    """A presigned GET URL for one object, for clients that read RustFS directly.

    Signing is local — no round trip to RustFS — so a URL comes back even for
    a key that does not exist; fetching it is what fails. ``filename`` sets
    the download name via response-content-disposition, RFC 5987-encoded
    since the names are frequently Korean.
    """
    params: dict[str, str] = {"Bucket": bucket, "Key": key}
    if filename:
        params["ResponseContentDisposition"] = (
            f"inline; filename*=UTF-8''{quote(filename)}"
        )
    return get_client().generate_presigned_url(
        "get_object", Params=params, ExpiresIn=expires_in
    )
