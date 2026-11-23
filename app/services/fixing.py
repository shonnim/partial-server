"""Rewriting two-up PDFs into one page per sheet.

Parts of the corpus were exported as spreads: one PDF sheet holds two
side-by-side pages, sometimes with a single cover sheet in front. Everything
downstream — parsing, page indexes, page crops — assumes one page per sheet,
so the Fixing page straightens these documents out before they are parsed.

The rule is per sheet, which covers both shapes the corpus has:

- every sheet two-up            → every sheet is split
- single cover, then two-up     → the cover passes through, the rest is split

A sheet counts as a *spread* when it is landscape (its displayed width clearly
exceeds its displayed height, rotation taken into account) — but only inside a
document where spreads are the majority of the sheets. A portrait report with
a handful of landscape sheets is carrying wide tables, not spreads, and must
not have them cut in half; the corpus's real two-up documents are landscape on
(almost) every sheet. A spread becomes two pages by cloning the sheet and
halving the media box — the content streams are untouched, so the rewrite is
cheap and lossless. Visual reading order is kept: the left half comes first.

The source PDF is never destroyed. Before the fix writes anything, the
previous bytes are copied to a sibling key (``{stem}_original.pdf``) and that
key is recorded on ``original_s3_key``; the fixed PDF then replaces the
previous one under the document's own key, so everything that reads the
document — the viewer, parsing, the RAG folder — picks up the fixed pages
without any key changing. Copying the backup back over the key undoes the
fix. Re-running a fix on an already-fixed document finds no spreads and
writes nothing, so the operation is idempotent.
"""

import posixpath
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from io import BytesIO

from pypdf import PdfReader, PdfWriter
from pypdf._page import PageObject
from pypdf.generic import RectangleObject
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.document import Document
from app.storage import copy_object, put_object, read_object

# Documents one Check or Fix click may cover. Each document's PDF is read in
# full, re-written, and uploaded, so keep an eye on request duration if this
# grows further.
MAX_FIX_BATCH = 200

# A PDF larger than this is refused rather than loaded into memory.
MAX_PDF_BYTES = 200 * 1024 * 1024

# How much wider than tall a displayed sheet must be to count as a spread.
# Portrait pages sit near 0.7; two portrait pages side by side near 1.4. The
# margin only guards against near-square covers.
_SPREAD_MIN_RATIO = 1.05

# The share of a document's sheets that must be spreads before the document
# counts as two-up at all. Measured on the corpus: real two-up documents are
# landscape on every sheet (or all but a single cover), while portrait
# reports carry a few percent of landscape sheets that are wide tables.
_MIN_SPREAD_SHARE = 0.5


class UnreadablePdf(ValueError):
    """The bytes could not be opened as a PDF this feature can rewrite."""


@dataclass(frozen=True)
class SpreadAnalysis:
    """What the sheets of one PDF look like."""

    sheets: int
    spreads: int

    @property
    def result_pages(self) -> int:
        """Pages the fixed PDF would have: each spread becomes two."""
        return self.sheets + self.spreads

    @property
    def spread_share(self) -> float:
        return self.spreads / self.sheets if self.sheets else 0.0

    @property
    def is_two_up(self) -> bool:
        """Whether this document as a whole reads as two pages per sheet."""
        return self.spreads > 0 and self.spread_share >= _MIN_SPREAD_SHARE

    @property
    def minority_landscape(self) -> bool:
        """Landscape sheets exist but the document is not two-up — wide
        tables in a portrait report, most of the time."""
        return self.spreads > 0 and not self.is_two_up

    @property
    def needs_fix(self) -> bool:
        return self.is_two_up


@dataclass(frozen=True)
class DocumentCheck:
    """What a Check click found out about one document."""

    doc_id: uuid.UUID
    filename: str
    fixed: bool = False
    analysis: SpreadAnalysis | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class DocumentFix:
    """What fixing one document did, or why it did nothing."""

    doc_id: uuid.UUID
    filename: str
    key: str | None = None
    pages_before: int = 0
    pages_after: int = 0
    spreads: int = 0
    # Why nothing was written although nothing went wrong either — an
    # already-single-page document, most of the time.
    skipped: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def wrote(self) -> bool:
        return self.ok and self.key is not None


@dataclass(frozen=True)
class DocumentPagesCheck:
    """One document's stored page count against what its PDF really holds."""

    doc_id: uuid.UUID
    filename: str
    db_pages: int | None = None
    real_pages: int | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def corrected(self) -> bool:
        """The row disagreed with the PDF and has been updated to match it."""
        return self.ok and self.db_pages != self.real_pages


@dataclass(frozen=True)
class PagesCheckResult:
    """One Check-pages click, document by document."""

    checks: Sequence[DocumentPagesCheck] = field(default_factory=tuple)

    @property
    def corrected(self) -> int:
        return sum(1 for c in self.checks if c.corrected)

    @property
    def matched(self) -> int:
        return sum(1 for c in self.checks if c.ok and not c.corrected)

    @property
    def failed(self) -> int:
        return sum(1 for c in self.checks if not c.ok)

    @property
    def corrections(self) -> list[DocumentPagesCheck]:
        """The rows whose stored count changed, for the tree to repaint."""
        return [c for c in self.checks if c.corrected]


@dataclass(frozen=True)
class CheckResult:
    """One Check click, document by document."""

    checks: Sequence[DocumentCheck] = field(default_factory=tuple)

    @property
    def with_spreads(self) -> int:
        return sum(1 for c in self.checks if c.analysis and c.analysis.needs_fix)

    @property
    def failed(self) -> int:
        return sum(1 for c in self.checks if not c.ok)


@dataclass(frozen=True)
class FixResult:
    """One Fix click, document by document."""

    fixes: Sequence[DocumentFix] = field(default_factory=tuple)

    @property
    def succeeded(self) -> int:
        return sum(1 for f in self.fixes if f.wrote)

    @property
    def skipped(self) -> int:
        return sum(1 for f in self.fixes if f.ok and not f.wrote)

    @property
    def failed(self) -> int:
        return sum(1 for f in self.fixes if not f.ok)

    @property
    def fixed_ids(self) -> list[uuid.UUID]:
        """The documents now stamped as fixed, for the tree to disable."""
        return [f.doc_id for f in self.fixes if f.wrote]


# ---------------------------------------------------------------------------
# The pure PDF rules
# ---------------------------------------------------------------------------


def _open(data: bytes) -> PdfReader:
    """Open PDF bytes, surfacing anything unreadable as UnreadablePdf."""
    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            # An empty owner password is common on generated PDFs; a real
            # password is not something this tool asks for.
            if not reader.decrypt(""):
                raise UnreadablePdf("PDF is password-protected")
        len(reader.pages)  # force the page tree to parse here, not later
    except UnreadablePdf:
        raise
    except Exception as exc:
        raise UnreadablePdf(f"not a readable PDF ({type(exc).__name__}: {exc})") from exc
    return reader


def _is_spread(page: PageObject) -> bool:
    """Whether this sheet is displayed clearly wider than tall."""
    box = page.mediabox
    width, height = float(box.width), float(box.height)
    if (page.rotation or 0) % 180 == 90:
        width, height = height, width
    return height > 0 and width / height >= _SPREAD_MIN_RATIO


def _halves(page: PageObject) -> tuple[RectangleObject, RectangleObject]:
    """The two half-sheet boxes, visual left half first.

    /Rotate swaps which media-box axis runs left-to-right on screen: at 90
    (clockwise) the unrotated y axis points right, so the visual left half is
    the *bottom* half of the box; 180 and 270 mirror the respective order.
    """
    box = page.mediabox
    x0, y0 = float(box.left), float(box.bottom)
    x1, y1 = float(box.right), float(box.top)
    rotation = (page.rotation or 0) % 360

    if rotation % 180 == 0:
        mid = (x0 + x1) / 2
        first = RectangleObject((x0, y0, mid, y1))
        second = RectangleObject((mid, y0, x1, y1))
    else:
        mid = (y0 + y1) / 2
        first = RectangleObject((x0, y0, x1, mid))
        second = RectangleObject((x0, mid, x1, y1))

    if rotation in (180, 270):
        first, second = second, first
    return first, second


def analyze_pdf(data: bytes) -> SpreadAnalysis:
    """Count the sheets and the spreads among them, without writing anything."""
    reader = _open(data)
    flags = [_is_spread(page) for page in reader.pages]
    return SpreadAnalysis(sheets=len(flags), spreads=sum(flags))


def unspread_pdf(data: bytes) -> tuple[bytes | None, SpreadAnalysis]:
    """Rewrite the PDF with every spread split into two pages.

    Returns ``(fixed_bytes, analysis)``, or ``(None, analysis)`` when there is
    no spread to split — the caller must not write anything in that case.
    """
    reader = _open(data)
    flags = [_is_spread(page) for page in reader.pages]
    analysis = SpreadAnalysis(sheets=len(flags), spreads=sum(flags))
    if not analysis.needs_fix:
        return None, analysis

    writer = PdfWriter()
    for page, spread in zip(reader.pages, flags, strict=True):
        if not spread:
            writer.add_page(page)
            continue
        # add_page clones the sheet into the writer, so the two copies hold
        # independent page dictionaries over the same content stream; halving
        # the boxes is all it takes to show one page each.
        for half in _halves(page):
            copy = writer.add_page(page)
            copy.mediabox = half
            copy.cropbox = half

    out = BytesIO()
    writer.write(out)
    return out.getvalue(), analysis


# ---------------------------------------------------------------------------
# The storage- and DB-facing batch
# ---------------------------------------------------------------------------


def backup_key_for(document: Document) -> str:
    """The sibling key the untouched source PDF is copied to before a fix.

    The fixed PDF replaces the object at the document's own key, so the
    previous bytes move aside first — the backup is what keeps a fix
    reversible. The document's key never changes, so neither does this one.
    """
    stem, _ = posixpath.splitext(document.s3_key)
    return f"{stem}_original.pdf"


def _read_pdf(document: Document) -> bytes:
    data, too_large = read_object(
        document.bucket, document.s3_key, max_bytes=MAX_PDF_BYTES
    )
    if too_large:
        raise UnreadablePdf(
            f"PDF exceeds the {MAX_PDF_BYTES // (1024 * 1024)} MB fixing limit"
        )
    return data


def _documents_by_id(
    db: Session, doc_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, Document]:
    documents = db.scalars(select(Document).where(Document.id.in_(list(doc_ids)))).all()
    return {d.id: d for d in documents}


def check_documents(db: Session, doc_ids: Sequence[uuid.UUID]) -> CheckResult:
    """Report, without writing anything, which selected PDFs hold spreads."""
    by_id = _documents_by_id(db, doc_ids)

    checks: list[DocumentCheck] = []
    for doc_id in doc_ids:
        document = by_id.get(doc_id)
        if document is None:
            continue  # the selection outlived the row; nothing to report on
        try:
            analysis = analyze_pdf(_read_pdf(document))
        except Exception as exc:  # noqa: BLE001 — recorded per document, never silent
            checks.append(
                DocumentCheck(
                    doc_id=document.id,
                    filename=document.filename,
                    fixed=document.fixed_at is not None,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        checks.append(
            DocumentCheck(
                doc_id=document.id,
                filename=document.filename,
                fixed=document.fixed_at is not None,
                analysis=analysis,
            )
        )

    return CheckResult(checks=checks)


def check_pages(db: Session, doc_ids: Sequence[uuid.UUID]) -> PagesCheckResult:
    """Count each selected PDF's real pages and correct rows that disagree.

    The PDF is the truth here: ``documents.pages`` came from registration and
    can drift when the object is replaced outside this tool. A row whose count
    differs is updated to the real count and committed on its own, so a
    failure later in the batch keeps the corrections already made. A document
    that cannot be read is recorded with its error and left untouched.
    """
    by_id = _documents_by_id(db, doc_ids)

    checks: list[DocumentPagesCheck] = []
    for doc_id in doc_ids:
        document = by_id.get(doc_id)
        if document is None:
            continue  # the selection outlived the row; nothing to report on
        try:
            real_pages = len(_open(_read_pdf(document)).pages)
        except Exception as exc:  # noqa: BLE001 — recorded per document, never silent
            checks.append(
                DocumentPagesCheck(
                    doc_id=document.id,
                    filename=document.filename,
                    db_pages=document.pages,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            continue

        check = DocumentPagesCheck(
            doc_id=document.id,
            filename=document.filename,
            db_pages=document.pages,
            real_pages=real_pages,
        )
        if check.corrected:
            document.pages = real_pages
            document.updated_at = func.now()
            db.commit()
        checks.append(check)

    return PagesCheckResult(checks=checks)


def _fix_one(document: Document) -> DocumentFix:
    """Rewrite one document's PDF and repoint the row at the result.

    The row mutation happens here; committing is the batch's job, after the
    bytes are already safely in RustFS.
    """
    fixed, analysis = unspread_pdf(_read_pdf(document))
    if fixed is None:
        reason = (
            f"only {analysis.spreads} of {analysis.sheets} sheets are landscape "
            "— wide tables, not a two-up document"
            if analysis.minority_landscape
            else "no spread sheets — already one page per sheet"
        )
        return DocumentFix(
            doc_id=document.id,
            filename=document.filename,
            pages_before=analysis.sheets,
            pages_after=analysis.sheets,
            skipped=reason,
        )

    # The untouched source moves aside before anything replaces it. The copy
    # is server-side, and a failure here stops the fix with the object still
    # intact. Only the first fix copies — the first backup is the one that
    # holds the true original, and a later fix must not overwrite it.
    backup = backup_key_for(document)
    if document.original_s3_key is None:
        copy_object(document.bucket, document.s3_key, document.bucket, backup)

    # The fixed PDF replaces the previous one under the document's own key,
    # so everything keyed on it — the viewer, parse runs, the RAG folder —
    # picks up the fixed pages without anything being renamed.
    put_object(
        document.bucket, document.s3_key, fixed, content_type="application/pdf"
    )

    if document.original_s3_key is None:
        document.original_s3_key = backup
    document.size_bytes = len(fixed)
    document.pages = analysis.result_pages
    document.fixed_at = func.now()
    document.updated_at = func.now()

    return DocumentFix(
        doc_id=document.id,
        filename=document.filename,
        key=document.s3_key,
        pages_before=analysis.sheets,
        pages_after=analysis.result_pages,
        spreads=analysis.spreads,
    )


def fix_documents(db: Session, doc_ids: Sequence[uuid.UUID]) -> FixResult:
    """Fix every named document, one at a time.

    A document that fails is recorded with its error and the batch moves on.
    The rewrite is idempotent — re-running the selection redoes nothing that
    already holds one page per sheet.
    """
    by_id = _documents_by_id(db, doc_ids)

    fixes: list[DocumentFix] = []
    for doc_id in doc_ids:
        document = by_id.get(doc_id)
        if document is None:
            continue  # the selection outlived the row; nothing to report on
        try:
            fix = _fix_one(document)
            # Postgres is the source of truth for fixing state, and each
            # document commits on its own: a failure later in the batch must
            # not take an earlier document's stamp down with it.
            if fix.wrote:
                db.commit()
            fixes.append(fix)
        except Exception as exc:  # noqa: BLE001 — recorded per document, never silent
            db.rollback()  # drop any half-applied row mutation of this document
            fixes.append(
                DocumentFix(
                    doc_id=document.id,
                    filename=document.filename,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )

    return FixResult(fixes=fixes)
