"""The one place that talks S3 to RustFS.

Routers and services go through this module. Keeping the client behind a
function is also what makes storage fixable in tests.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from app.config import get_settings

# Bytes per chunk when streaming an object body to the client.
_STREAM_CHUNK = 64 * 1024

# Error codes RustFS returns when the key (or the bucket) does not exist.
_MISSING_CODES = {"NoSuchKey", "NoSuchBucket", "404"}


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
