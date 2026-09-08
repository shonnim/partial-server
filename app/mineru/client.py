"""A minimal client for the MinerU parsing service.

MinerU parses a whole document asynchronously: a POST returns a task id at
once, the task is polled until it settles, and the output is fetched as a zip.
The four calls the Parsing page needs are wrapped here and nothing more.

The service's replies carry ``status_url`` and ``result_url`` fields, but
those are built from the host the service thinks it has — without the port
and path prefix it is actually reached through — so URLs are always derived
from the configured base here and the reply's copies are ignored.

Every failure — transport, HTTP status, a body that is not the JSON expected —
comes back as MineruError with a message worth showing a human. Nothing here
retries: the caller is a browser loop that already polls on a clock.
"""

import json
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings

# The parse mode every document is sent with, and the language hint.
BACKEND = "vlm-engine"
LANG = "korean"

# Task states the service reports. The first two are still in flight.
PENDING = "pending"
PROCESSING = "processing"
COMPLETED = "completed"
FAILED = "failed"


class MineruError(RuntimeError):
    """The parsing service could not be reached, or did not answer usefully."""


@dataclass(frozen=True)
class MineruHealth:
    """What the health endpoint reports, trimmed to the fields shown."""

    status: str
    version: str | None
    queued_tasks: int
    processing_tasks: int
    completed_tasks: int
    failed_tasks: int
    max_concurrent_requests: int

    @property
    def healthy(self) -> bool:
        return self.status == "healthy"


@dataclass(frozen=True)
class MineruTask:
    """One task as the service describes it, on submit and on every poll."""

    task_id: str
    status: str
    backend: str | None
    queued_ahead: int | None
    error: str | None

    @property
    def in_flight(self) -> bool:
        return self.status in (PENDING, PROCESSING)

    @property
    def completed(self) -> bool:
        return self.status == COMPLETED

    @property
    def failed(self) -> bool:
        return self.status == FAILED


def _url(path: str) -> str:
    return f"{get_settings().mineru_base_url.rstrip('/')}/{path.lstrip('/')}"


def _request(method: str, path: str, *, timeout: float, **kwargs: Any) -> httpx.Response:
    try:
        return httpx.request(method, _url(path), timeout=timeout, **kwargs)
    except httpx.TimeoutException as exc:
        raise MineruError(
            f"the parsing service did not answer within {timeout:.0f}s"
        ) from exc
    except httpx.HTTPError as exc:
        raise MineruError(f"could not reach the parsing service: {exc}") from exc


def _json_object(response: httpx.Response, *, expected: tuple[int, ...]) -> dict[str, Any]:
    if response.status_code not in expected:
        raise MineruError(
            f"parsing service returned HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )
    try:
        body = response.json()
    except json.JSONDecodeError as exc:
        raise MineruError(f"the parsing service's reply was not JSON: {exc.msg}") from exc
    if not isinstance(body, dict):
        raise MineruError("the parsing service's reply was not a JSON object")
    return body


def _int(body: dict[str, Any], key: str) -> int:
    value = body.get(key)
    return value if isinstance(value, int) else 0


def _task(body: dict[str, Any]) -> MineruTask:
    task_id = body.get("task_id")
    status = body.get("status")
    if not isinstance(task_id, str) or not isinstance(status, str):
        raise MineruError("the parsing service's reply named no task_id or status")
    error = body.get("error")
    queued = body.get("queued_ahead")
    return MineruTask(
        task_id=task_id,
        status=status,
        backend=body.get("backend") if isinstance(body.get("backend"), str) else None,
        queued_ahead=queued if isinstance(queued, int) else None,
        error=str(error) if error else None,
    )


def health() -> MineruHealth:
    """Ask the service how it is doing."""
    settings = get_settings()
    response = _request("GET", "health", timeout=settings.mineru_poll_timeout_seconds)
    body = _json_object(response, expected=(200,))
    status = body.get("status")
    version = body.get("version")
    return MineruHealth(
        status=status if isinstance(status, str) else "unknown",
        version=str(version) if version is not None else None,
        queued_tasks=_int(body, "queued_tasks"),
        processing_tasks=_int(body, "processing_tasks"),
        completed_tasks=_int(body, "completed_tasks"),
        failed_tasks=_int(body, "failed_tasks"),
        max_concurrent_requests=_int(body, "max_concurrent_requests"),
    )


def submit_document(pdf: bytes, filename: str) -> MineruTask:
    """Queue a whole PDF for parsing. Returns the task as first reported."""
    settings = get_settings()
    response = _request(
        "POST",
        "tasks",
        timeout=settings.mineru_submit_timeout_seconds,
        files={"files": (filename, pdf, "application/pdf")},
        data={
            "response_format_zip": "true",
            "backend": BACKEND,
            "return_content_list": "true",
            "return_images": "true",
            "return_md": "true",
            "lang_list": LANG,
        },
    )
    return _task(_json_object(response, expected=(200, 202)))


def task_status(task_id: str) -> MineruTask:
    """Where a task stands now."""
    settings = get_settings()
    response = _request(
        "GET", f"tasks/{task_id}", timeout=settings.mineru_poll_timeout_seconds
    )
    if response.status_code == 404:
        raise MineruError(f"task {task_id} is unknown to the parsing service")
    return _task(_json_object(response, expected=(200,)))


def download_result(task_id: str) -> bytes:
    """The zip a completed task produced."""
    settings = get_settings()
    response = _request(
        "GET",
        f"tasks/{task_id}/result",
        timeout=settings.mineru_result_timeout_seconds,
    )
    if response.status_code != 200:
        raise MineruError(
            f"parsing service returned HTTP {response.status_code} for the result: "
            f"{response.text[:300]}"
        )
    if not response.content:
        raise MineruError("the parsing service returned an empty result")
    return response.content
