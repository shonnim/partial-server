"""Sending documents through the MinerU VLM engine and storing what comes back.

A run is three kinds of request from the browser, each about one document:

1. **submit** — the PDF is read from RustFS and posted to MinerU, and the
   document's ``parsed_docs`` row records the task id with status PROCESS.
   The service answers as soon as the task is queued, so the browser sends
   the next document right after.
2. **poll** — every few seconds each task in flight is asked where it stands.
   While it is pending or processing nothing changes here.
3. **finish** — the first poll that finds a task completed downloads the zip,
   clears the document's previous parse directory in the derived bucket, and
   writes the ``vlm`` folder's contents in its place. The row moves to
   COMPLETED with the size of what was stored.

One document per request, on purpose: a VLM pass over a long document is
minutes, and the browser driving the queue is what keeps a batch of fifty from
being one fifty-minute HTTP request.

Nothing here guesses. A task the service reports as failed, a zip without a
``vlm`` folder, a storage write that does not go through — each is recorded as
FAILED on the row with the reason, never as a quietly empty result.
"""

import mimetypes
import posixpath
import time
import uuid
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.mineru import (
    BACKEND,
    MineruError,
    MineruTask,
    download_result,
    submit_document,
    task_status,
)
from app.models.document import Document, DocumentStatus
from app.models.parsed_doc import ParsedDoc
from app.services.parse_artifacts import latest_parsed_doc, run_prefix
from app.storage import (
    ObjectNotFound,
    delete_objects,
    list_all_objects,
    put_object,
    read_object,
)

# How many documents one click may queue. Each one is a full VLM parse, so a
# selection is capped rather than left to grow into an all-night job.
MAX_PARSE_BATCH = 100

# How often the browser asks after the tasks in flight.
POLL_SECONDS = 10

# How often the status box asks the service how it is doing, under Auto check.
HEALTH_SECONDS = 5

# The whole PDF is uploaded to the parsing service, so it has to fit in memory
# here first. Kept in step with the 500 MB upload ceiling in uploads.py.
MAX_PDF_BYTES = 500 * 1024 * 1024

# The folder inside the result zip whose contents become the run directory.
_RESULT_FOLDER = "vlm"

# Content types for what the zip holds. Anything else falls back to the
# mimetypes table, and to octet-stream after that.
_CONTENT_TYPES = {
    ".md": "text/markdown; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


class ParseRunError(RuntimeError):
    """A step of the run could not be completed."""


@dataclass(frozen=True)
class ParseCandidate:
    """One selected document and whether the run will take it."""

    id: uuid.UUID
    filename: str
    #: Backend and status of the latest parse, None when never parsed.
    backend: str | None
    status: DocumentStatus | None
    #: Why the run leaves it out; None when it is eligible.
    reason: str | None = None

    @property
    def eligible(self) -> bool:
        return self.reason is None


@dataclass(frozen=True)
class ParseQueue:
    candidates: Sequence[ParseCandidate]

    @property
    def eligible(self) -> list[ParseCandidate]:
        return [c for c in self.candidates if c.eligible]

    @property
    def skipped(self) -> list[ParseCandidate]:
        return [c for c in self.candidates if not c.eligible]


@dataclass(frozen=True)
class RunStep:
    """What one request in a document's run found out."""

    id: uuid.UUID
    filename: str
    #: One of pending, processing, completed, failed.
    state: str
    task_id: str | None = None
    queued_ahead: int | None = None
    error: str | None = None
    #: Filled in when the run finished: what was stored.
    artifacts: int | None = None
    size_bytes: int | None = None
    seconds: float | None = None
    #: The task was already running from an earlier click and was picked up
    #: rather than submitted again.
    resumed: bool = False

    @property
    def in_flight(self) -> bool:
        return self.state in ("pending", "processing")

    @property
    def ok(self) -> bool:
        return self.state == "completed"


def needs_parse(backend: str | None) -> bool:
    """Whether a document with this latest backend still needs the VLM engine.

    Never parsed and parsed by another engine both qualify; only a document
    that has already been through ``vlm-engine`` is left alone.
    """
    return backend != BACKEND


def build_queue(db: Session, doc_ids: Sequence[uuid.UUID]) -> ParseQueue:
    """Decide, for each selected document, whether the run will take it."""
    if not doc_ids:
        return ParseQueue(candidates=())

    documents = db.scalars(
        select(Document).where(Document.id.in_(doc_ids)).order_by(Document.filename)
    ).all()

    candidates = []
    for doc in documents:
        latest = latest_parsed_doc(db, doc.id)
        backend = latest.backend if latest else None
        status = latest.status if latest else None
        reason = None if needs_parse(backend) else f"already parsed with {BACKEND}"
        candidates.append(
            ParseCandidate(
                id=doc.id,
                filename=doc.filename,
                backend=backend,
                status=status,
                reason=reason,
            )
        )
    return ParseQueue(candidates=candidates)


def _folder_of(document: Document) -> str | None:
    """The folder a document's run directory goes under."""
    if document.folder_path:
        return document.folder_path.strip("/")
    folder = posixpath.dirname(document.s3_key)
    return folder or None


def _row_for(db: Session, document: Document) -> ParsedDoc:
    """The row a run writes to: the latest one, or a new one for a first parse.

    A fresh row inherits the derived bucket from settings and the folder from
    the document, which is where every run directory in the bucket already
    sits.
    """
    row = latest_parsed_doc(db, document.id)
    if row is not None:
        if not row.bucket:
            row.bucket = get_settings().bucket_derived
        if not row.folder_path:
            row.folder_path = _folder_of(document)
        return row

    row = ParsedDoc(
        doc_id=document.id,
        status=DocumentStatus.PROCESS,
        bucket=get_settings().bucket_derived,
        folder_path=_folder_of(document),
        category=document.category,
    )
    db.add(row)
    db.flush()
    return row


def _row_status(task: MineruTask) -> DocumentStatus:
    if task.completed:
        return DocumentStatus.COMPLETED
    if task.failed:
        return DocumentStatus.FAILED
    return DocumentStatus.PROCESS


def _record_task(db: Session, row: ParsedDoc, task: MineruTask) -> None:
    row.task_id = task.task_id
    row.status = _row_status(task)
    row.backend = task.backend or BACKEND
    row.updated_at = datetime.now(UTC)
    db.commit()


def _record_failure(db: Session, row: ParsedDoc) -> None:
    row.status = DocumentStatus.FAILED
    row.updated_at = datetime.now(UTC)
    db.commit()


def _step(document: Document, task: MineruTask, **extra: Any) -> RunStep:
    fields: dict[str, Any] = {
        "state": task.status,
        "task_id": task.task_id,
        "queued_ahead": task.queued_ahead,
        "error": task.error,
    }
    fields.update(extra)
    return RunStep(id=document.id, filename=document.filename, **fields)


def _failed_step(document: Document, reason: str, **extra: Any) -> RunStep:
    return RunStep(
        id=document.id, filename=document.filename, state="failed", error=reason, **extra
    )


def _resumable(row: ParsedDoc) -> MineruTask | None:
    """A task an earlier click started and never finished, if it still exists.

    Re-submitting would upload the PDF again and leave the first task to
    complete unobserved, so a row still marked PROCESS is checked against the
    service first. A task the service no longer knows is simply gone.
    """
    if row.status is not DocumentStatus.PROCESS or not row.task_id:
        return None
    if row.backend and row.backend != BACKEND:
        return None
    try:
        task = task_status(row.task_id)
    except MineruError:
        return None
    return task if task.in_flight or task.completed else None


def submit(db: Session, document: Document) -> RunStep:
    """Send one document to the parsing service and record the task."""
    row = _row_for(db, document)

    if (task := _resumable(row)) is not None:
        _record_task(db, row, task)
        return _step(document, task, resumed=True)

    try:
        pdf, oversized = read_object(
            document.bucket, document.s3_key, max_bytes=MAX_PDF_BYTES
        )
    except ObjectNotFound as exc:
        return _failed_step(document, f"PDF missing from storage: {exc}")
    if oversized:
        return _failed_step(
            document, f"PDF is larger than {MAX_PDF_BYTES // (1024 * 1024)} MB"
        )

    began = time.monotonic()
    try:
        task = submit_document(pdf, document.filename)
    except MineruError as exc:
        return _failed_step(document, str(exc), seconds=time.monotonic() - began)

    _record_task(db, row, task)
    if task.failed:
        return _step(document, task, error=task.error or "the service rejected the task")
    return _step(document, task, seconds=time.monotonic() - began)


def poll(db: Session, document: Document, task_id: str) -> RunStep:
    """Check a task; when it has completed, store its output and close the row."""
    row = _row_for(db, document)

    try:
        task = task_status(task_id)
    except MineruError as exc:
        # A poll that cannot reach the service is not a failed parse; the
        # browser asks again on the next tick, so the row is left as it is.
        return RunStep(
            id=document.id,
            filename=document.filename,
            state="processing",
            task_id=task_id,
            error=str(exc),
        )

    if task.in_flight:
        return _step(document, task)

    if task.failed:
        _record_task(db, row, task)
        return _step(
            document, task, error=task.error or "the parsing service reported failure"
        )

    began = time.monotonic()
    try:
        artifacts, size = _store_result(row, document, task.task_id)
    except (MineruError, ParseRunError) as exc:
        _record_failure(db, row)
        return _step(
            document, task, state="failed", error=str(exc), seconds=time.monotonic() - began
        )

    row.task_id = task.task_id
    row.backend = task.backend or BACKEND
    row.status = DocumentStatus.COMPLETED
    row.size_bytes = size
    row.updated_at = datetime.now(UTC)
    db.commit()

    return _step(
        document,
        task,
        artifacts=artifacts,
        size_bytes=size,
        seconds=time.monotonic() - began,
    )


def _result_entries(archive: zipfile.ZipFile) -> list[tuple[str, zipfile.ZipInfo]]:
    """``(relative path, entry)`` for every file under the zip's ``vlm`` folder.

    The zip nests the output as ``{name}/vlm/...``. Only what is under ``vlm``
    is kept, with that prefix stripped, so the run directory holds the
    markdown, the content lists and ``images/`` directly.
    """
    entries = []
    for info in archive.infolist():
        if info.is_dir():
            continue
        parts = info.filename.split("/")
        if _RESULT_FOLDER not in parts:
            continue
        relative = "/".join(parts[parts.index(_RESULT_FOLDER) + 1 :])
        if not relative or ".." in relative.split("/"):
            continue
        entries.append((relative, info))
    return entries


def _content_type(name: str) -> str:
    ext = posixpath.splitext(name)[1].lower()
    if ext in _CONTENT_TYPES:
        return _CONTENT_TYPES[ext]
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


def _store_result(row: ParsedDoc, document: Document, task_id: str) -> tuple[int, int]:
    """Fetch the result zip and make it the document's run directory.

    The download comes first: if the zip cannot be fetched or holds nothing
    usable, the previous output is still there. Only once the new output is in
    hand is the old directory cleared and the new one written.

    Returns ``(objects written, bytes written)``.
    """
    if not row.bucket:
        raise ParseRunError("the parse row records no bucket to write into")

    payload = download_result(task_id)
    try:
        archive = zipfile.ZipFile(BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise ParseRunError(f"the result was not a zip file: {exc}") from exc

    with archive:
        entries = _result_entries(archive)
        if not entries:
            sample = ", ".join(archive.namelist()[:5])
            raise ParseRunError(
                f"the result zip has no {_RESULT_FOLDER!r} folder (entries: {sample})"
            )

        prefix = run_prefix(row, document)
        try:
            old = list_all_objects(row.bucket, prefix)
            delete_objects(row.bucket, (o.key for o in old))
        except ObjectNotFound:
            pass  # nothing stored yet: a first parse
        except Exception as exc:  # boto raises many types; the message is what matters
            raise ParseRunError(f"could not clear {row.bucket}/{prefix}: {exc}") from exc

        total = 0
        for relative, info in entries:
            data = archive.read(info)
            try:
                put_object(
                    row.bucket,
                    prefix + relative,
                    data,
                    content_type=_content_type(relative),
                )
            except Exception as exc:
                raise ParseRunError(
                    f"could not write {row.bucket}/{prefix}{relative}: {exc}"
                ) from exc
            total += len(data)

    return len(entries), total
