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


def open_object(bucket: str, key: str) -> ObjectStream:
    """Open an object for streaming.

    Raises ObjectNotFound if the key is absent, so a missing blob surfaces as a
    404 rather than a 500.
    """
    try:
        response = get_client().get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in _MISSING_CODES:
            raise ObjectNotFound(bucket, key) from exc
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
