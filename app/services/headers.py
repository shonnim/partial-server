"""Deciding whether a document carries a table of contents.

The verdict lives on ``documents.has_header`` and is reached by reading the
content list a parse run produced and looking for the words a table of contents
is introduced with. A document whose content list cannot be read is *not*
recorded as having no header — that would make a missing parse indistinguishable from a
document that genuinely has no contents page. It stays UNCHECKED and is
reported as a failure.

The check is idempotent by design: a document already marked YES or NO is
skipped, so re-running over an overlapping selection costs nothing and cannot
flip an earlier verdict.
"""

import enum
import json
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.document import Document, HasHeader
from app.services.parse_artifacts import (
    MAX_INLINE_BYTES,
    ArtifactKind,
    ArtifactUnavailable,
    resolve_artifact,
)
from app.storage import ObjectNotFound, read_object

# What a table of contents is called in this corpus. Korean headings are
# written both with and without a space between the syllables, and the two are
# different strings, so both spellings are listed.
HEADER_MARKERS: tuple[str, ...] = ("목 차", "목차", "차 례", "차례", "contents")

# How many documents one click may check. The read is one RustFS round-trip per
# document inside an HTTP request, so the selection is capped rather than left
# to grow into a batch job.
MAX_CHECK_BATCH = 100


class CheckOutcome(str, enum.Enum):
    """What happened to one document during a check."""

    DECIDED = "decided"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class DocumentCheck:
    """The result for a single document."""

    id: uuid.UUID
    filename: str
    outcome: CheckOutcome
    has_header: HasHeader
    #: The word that settled a YES verdict, so the decision can be argued with.
    marker: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class CheckReport:
    checks: Sequence[DocumentCheck]

    def _count(self, outcome: CheckOutcome) -> int:
        return sum(1 for c in self.checks if c.outcome is outcome)

    @property
    def decided(self) -> int:
        return self._count(CheckOutcome.DECIDED)

    @property
    def skipped(self) -> int:
        return self._count(CheckOutcome.SKIPPED)

    @property
    def failed(self) -> int:
        return self._count(CheckOutcome.FAILED)

    @property
    def yes(self) -> int:
        return sum(
            1
            for c in self.checks
            if c.outcome is CheckOutcome.DECIDED and c.has_header is HasHeader.YES
        )

    @property
    def no(self) -> int:
        return sum(
            1
            for c in self.checks
            if c.outcome is CheckOutcome.DECIDED and c.has_header is HasHeader.NO
        )

    @property
    def failures(self) -> list[DocumentCheck]:
        return [c for c in self.checks if c.outcome is CheckOutcome.FAILED]

    @property
    def updates(self) -> dict[str, str]:
        """Document id → new badge value, for the rows already on the page."""
        return {
            str(c.id): c.has_header.value
            for c in self.checks
            if c.outcome is CheckOutcome.DECIDED
        }


def find_marker(haystack: str) -> str | None:
    """The first contents marker present in ``haystack``, or None.

    Matching is case-insensitive, which only affects the Latin marker; the
    Korean ones are unaffected by case folding.
    """
    folded = haystack.casefold()
    for marker in HEADER_MARKERS:
        if marker.casefold() in folded:
            return marker
    return None


def load_content_list(db: Session, document: Document) -> tuple[Any, str | None]:
    """A document's latest content list, decoded, or why there is none.

    Returns ``(blocks, None)`` on success and ``(None, reason)`` otherwise. The
    reason is written for the person reading the failure panel.
    """
    try:
        artifact = resolve_artifact(db, document, ArtifactKind.JSON)
    except ArtifactUnavailable as exc:
        return None, str(exc)

    try:
        raw, oversized = read_object(
            artifact.bucket, artifact.key, max_bytes=MAX_INLINE_BYTES
        )
    except ObjectNotFound as exc:
        return None, f"content list missing from storage: {exc}"

    if oversized:
        return None, f"content list is larger than {MAX_INLINE_BYTES} bytes"

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return None, f"content list is not valid UTF-8 at byte {exc.start}"

    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, f"content list is not valid JSON: {exc.msg} (line {exc.lineno})"


def _read_content_list(db: Session, document: Document) -> tuple[str | None, str | None]:
    """A document's latest content list as searchable text, or why there is none.

    The JSON is parsed and re-serialised rather than searched as it arrived: a
    writer that escaped non-ASCII would leave the Korean markers on disk as
    escape sequences, which no literal search would ever match. Re-dumping with
    ensure_ascii off normalises both spellings to the same characters.
    """
    parsed, error = load_content_list(db, document)
    if error is not None:
        return None, error
    return json.dumps(parsed, ensure_ascii=False), None


def contents_page_index(blocks: Any) -> int | None:
    """The ``page_idx`` of the first block that carries a contents marker.

    Blocks are searched the same way the has_header check searches the whole
    list — as re-dumped JSON — so a document that was decided YES always
    yields a page here, unless the block that decided it has no page index.
    """
    if not isinstance(blocks, list):
        return None

    for block in blocks:
        if not isinstance(block, dict):
            continue
        if find_marker(json.dumps(block, ensure_ascii=False)) is None:
            continue
        page = block.get("page_idx")
        if isinstance(page, int) and not isinstance(page, bool) and page >= 0:
            return page
    return None


# A run of digits not touching another digit, so page "1" in "1 •" matches
# while "1" in "12" does not.
_NUMBER_TOKEN = re.compile(r"(?<!\d)\d+(?!\d)")


def page_number_index(blocks: Any) -> dict[int, int]:
    """Printed page number → ``page_idx`` of the block that carries it.

    Built from the ``page_number`` blocks of a content list. The text of such
    a block is whatever the parser read off the page — "12", "- 12 -",
    "12 •" — so every whole number in it is taken as a printed page number.
    The first block to carry a number wins; a number printed twice (a running
    header and a footer on the same page, or a two-up parse) keeps its
    earliest page index.
    """
    index: dict[int, int] = {}
    if not isinstance(blocks, list):
        return index

    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "page_number":
            continue
        page = block.get("page_idx")
        if not isinstance(page, int) or isinstance(page, bool) or page < 0:
            continue
        text = block.get("text")
        if not isinstance(text, str):
            continue
        for token in _NUMBER_TOKEN.findall(text):
            index.setdefault(int(token), page)
    return index


def check_has_header(db: Session, doc_ids: Sequence[uuid.UUID]) -> CheckReport:
    """Settle ``has_header`` for every still-UNCHECKED document in the selection.

    Documents already marked YES or NO are reported as skipped and left alone.
    """
    if not doc_ids:
        return CheckReport(checks=[])

    documents = db.scalars(
        select(Document).where(Document.id.in_(list(doc_ids))).order_by(
            Document.filename, Document.id
        )
    ).all()

    checks: list[DocumentCheck] = []
    for document in documents:
        if document.has_header is not HasHeader.UNCHECKED:
            checks.append(
                DocumentCheck(
                    id=document.id,
                    filename=document.filename,
                    outcome=CheckOutcome.SKIPPED,
                    has_header=document.has_header,
                )
            )
            continue

        content, error = _read_content_list(db, document)
        if content is None:
            checks.append(
                DocumentCheck(
                    id=document.id,
                    filename=document.filename,
                    outcome=CheckOutcome.FAILED,
                    has_header=HasHeader.UNCHECKED,
                    error=error,
                )
            )
            continue

        marker = find_marker(content)
        document.has_header = HasHeader.YES if marker else HasHeader.NO
        checks.append(
            DocumentCheck(
                id=document.id,
                filename=document.filename,
                outcome=CheckOutcome.DECIDED,
                has_header=document.has_header,
                marker=marker,
            )
        )

    db.commit()
    return CheckReport(checks=checks)
