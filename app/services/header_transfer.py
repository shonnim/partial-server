"""Moving a document's TOC between the RAG files bucket and ``headers``.

The Transform page copies each document's folder into the RAG bucket with a
small ``{stem}_metadata.json`` beside the PDF. An enrichment pipeline outside
this server then writes the document's table of contents into that JSON, with
a content-list page index range per heading. The Transfer pop-up on the
Headers page shows that TOC next to whatever the ``headers`` table already
holds, and on request writes either side into the other: ``apply_toc`` takes
the bucket's TOC into the table, ``apply_headers_to_bucket`` takes the stored
headers into the metadata JSON.

The two directions write differently. Toward the table, an upsert, because
``header_contents`` rows hang off header ids (and are CASCADE-deleted with
them): an existing header the TOC also names is updated in place — title kept,
page numbers and tree position taken from the TOC — only a heading the table
does not have is inserted, and what the TOC never names is left untouched and
reported. Toward the bucket, a replace: the stored headers *are* the curated
truth, so the metadata JSON's ``toc`` array is rebuilt from them wholesale and
the pipeline's old TOC is discarded — though every field outside the TOC that
the pipeline wrote stays. Running the same transfer twice changes nothing the
second time, in either direction.
"""

import difflib
import json
import posixpath
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from itertools import count
from typing import Any, NamedTuple

import pydantic
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.document import Document
from app.models.header import Header
from app.schemas.header_transfer import TocEntry, TocMetadata

# The same reduction extraction uses to tell whether two spellings of a title
# are the same heading, and the same one-box validation the outline editor
# uses; the Transfer pop-up must agree with both.
from app.services.header_extraction import MAX_LEVEL, _edited_index, _title_key
from app.services.transform import dest_prefix_for
from app.storage import ObjectNotFound, put_object, read_object

# A metadata JSON is a few KB of bookkeeping plus one TOC; a megabyte of it is
# not a metadata file.
MAX_METADATA_BYTES = 2 * 1024 * 1024


class TransferError(RuntimeError):
    """The bucket's TOC could not be read for this document."""


@dataclass(frozen=True)
class TocNode:
    """One TOC heading, levelled and ready to show or to save.

    ``seq`` is the heading's preorder position in the TOC, the identity the
    pop-up's bucket editor sends an edit back under — the metadata JSON gives
    its entries no ids of their own.
    """

    title: str
    level: int
    start_page: int | None
    start_page_idx: int | None
    end_page_idx: int | None
    matched: bool
    seq: int = 0
    children: tuple["TocNode", ...] = ()


@dataclass(frozen=True)
class BucketToc:
    """The TOC as the metadata JSON holds it, plus where it came from."""

    bucket: str
    key: str
    roots: tuple[TocNode, ...]
    confidence: float | None
    method: str | None
    valid: bool | None
    warnings: tuple[str, ...]

    @property
    def total(self) -> int:
        def count(nodes: Sequence[TocNode]) -> int:
            return sum(1 + count(n.children) for n in nodes)

        return count(self.roots)


@dataclass(frozen=True)
class TransferOutcome:
    """What applying one bucket TOC to the ``headers`` table did."""

    updated: int
    inserted: int
    # Matched rows whose stored values already equalled the TOC's.
    unchanged: int
    # Stored headers the TOC never named, left exactly as they were.
    untouched: int


def metadata_key(document: Document) -> str:
    """Where the document's metadata JSON sits in the RAG bucket.

    The same key the Transform page wrote it to: the document's migrated
    folder, derived from its object key, never from a user-supplied name.
    """
    stem = posixpath.splitext(document.filename)[0]
    return f"{dest_prefix_for(document)}{stem}_metadata.json"


def _nodes(
    entries: Sequence[TocEntry], level: int, counter: Iterator[int]
) -> tuple[TocNode, ...]:
    nodes = []
    for entry in entries:
        title = entry.header.strip()
        if not title:
            continue
        # seq must be claimed before the recursion: preorder, parent first.
        seq = next(counter)
        nodes.append(
            TocNode(
                title=title,
                level=level,
                start_page=entry.page_num,
                start_page_idx=entry.page_idx,
                end_page_idx=entry.page_idx_end,
                matched=entry.matched,
                seq=seq,
                children=_nodes(entry.children, min(level + 1, MAX_LEVEL), counter),
            )
        )
    return tuple(nodes)


def _read_metadata(document: Document) -> tuple[dict[str, Any], str, str]:
    """The metadata JSON as a raw dict, plus the bucket and key it came from.

    Raw, not a pydantic model, because the editor writes this dict back and
    must carry every field the enrichment pipeline put there — a model with
    ``extra="ignore"`` would silently drop them. Every way the file can
    disappoint — absent, oversized, unreadable, written for another document —
    is a TransferError with a message fit for the pop-up.
    """
    bucket = get_settings().bucket_rag_files
    key = metadata_key(document)

    try:
        data, too_large = read_object(bucket, key, max_bytes=MAX_METADATA_BYTES)
    except ObjectNotFound:
        raise TransferError(
            f"No metadata JSON at {bucket}/{key} — migrate this document on the "
            "Transform page first."
        ) from None
    if too_large:
        raise TransferError(
            f"{bucket}/{key} is larger than {MAX_METADATA_BYTES:,} bytes; "
            "that is not a metadata file."
        )

    try:
        payload = json.loads(data)
    except ValueError as exc:
        raise TransferError(f"{bucket}/{key} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise TransferError(f"{bucket}/{key} does not hold a JSON object.")

    # The id check is what "by the doc id and s3_key" buys: a folder whose
    # metadata names another document would otherwise write that document's
    # TOC onto this one.
    found = payload.get("id")
    if found and str(found) != str(document.id):
        raise TransferError(
            f"{bucket}/{key} belongs to document {found}, not "
            f"{document.id} — the migrated folder does not match this row."
        )

    return payload, bucket, key


def load_bucket_toc(document: Document) -> BucketToc:
    """Read the document's TOC from its metadata JSON in the RAG bucket.

    A file without a usable TOC is a TransferError with a message fit for the
    pop-up, never an empty TOC.
    """
    payload, bucket, key = _read_metadata(document)

    try:
        metadata = TocMetadata.model_validate(payload)
    except pydantic.ValidationError as exc:
        raise TransferError(f"{bucket}/{key} could not be read: {exc}") from exc

    roots = _nodes(metadata.toc, level=1, counter=count())
    if not roots:
        raise TransferError(
            "The metadata JSON has no TOC yet"
            + ("" if metadata.has_toc else " (has_toc is false)")
            + " — run the enrichment pipeline on it first."
        )

    return BucketToc(
        bucket=bucket,
        key=key,
        roots=roots,
        confidence=metadata.toc_confidence,
        method=metadata.toc_method,
        valid=metadata.toc_valid,
        warnings=tuple(metadata.toc_warnings),
    )


def pair_with_existing(
    roots: Sequence[TocNode], existing: Sequence[Header]
) -> list[tuple[TocNode, TocNode | None, Header | None]]:
    """Each TOC node, preorder, with its parent node and its matched row.

    Matching is by title key within the document, first unclaimed row first,
    so a title that appears twice pairs in reading order. A node with no match
    is paired with None: that is the row the apply step inserts.
    """
    pool: dict[str, list[Header]] = {}
    for row in existing:
        pool.setdefault(_title_key(row.header), []).append(row)

    claimed: set[int] = set()

    def claim(title: str) -> Header | None:
        for row in pool.get(_title_key(title), []):
            if row.id not in claimed:
                claimed.add(row.id)
                return row
        return None

    pairs: list[tuple[TocNode, TocNode | None, Header | None]] = []

    def walk(nodes: Sequence[TocNode], parent: TocNode | None) -> None:
        for node in nodes:
            pairs.append((node, parent, claim(node.title)))
            walk(node.children, node)

    walk(roots, None)
    return pairs


# ---- showing the differences -------------------------------------------------


@dataclass(frozen=True)
class Segment:
    """A run of a stored title that agrees with the TOC's spelling, or not."""

    text: str
    differs: bool


@dataclass(frozen=True)
class HeaderDiff:
    """How one stored header disagrees with the bucket TOC, for the pop-up.

    ``segments`` is the stored title cut into runs; the differing runs are what
    the Stored pane paints red. The three flags mark the page pills the same
    way. A header the TOC does not name at all is ``missing``: its whole title
    is one differing run, and a transform would leave the row untouched.
    """

    segments: tuple[Segment, ...]
    page_differs: bool
    start_idx_differs: bool
    end_idx_differs: bool
    missing: bool = False


def title_segments(toc_title: str, stored_title: str) -> tuple[Segment, ...]:
    """The stored title as runs of agreement and disagreement with the TOC's.

    Character-level, so a single changed digit or a fullwidth numeral reads as
    exactly that character. Only what the stored title *has* can be painted: a
    character the TOC has and the stored title lacks shows up as nothing here
    (the pairing already guarantees the two reduce to the same key, so such
    gaps are spacing and punctuation, not words).
    """
    matcher = difflib.SequenceMatcher(None, toc_title, stored_title, autojunk=False)
    segments: list[Segment] = []
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if j1 == j2:
            continue
        differs = tag != "equal"
        if segments and segments[-1].differs == differs:
            segments[-1] = Segment(segments[-1].text + stored_title[j1:j2], differs)
        else:
            segments.append(Segment(stored_title[j1:j2], differs))
    return tuple(segments)


def diff_against_toc(
    roots: Sequence[TocNode], existing: Sequence[Header]
) -> dict[int, HeaderDiff]:
    """Each stored header's disagreement with the TOC, keyed by header id.

    The pairing is the same one the apply step uses, so what reads red here is
    exactly what a transform would change — or, for a missing row, refuse to
    touch.
    """
    diffs: dict[int, HeaderDiff] = {}
    for node, _parent, row in pair_with_existing(roots, existing):
        if row is None:
            continue
        diffs[row.id] = HeaderDiff(
            segments=title_segments(node.title, row.header),
            page_differs=row.start_page != node.start_page,
            start_idx_differs=row.start_page_idx != node.start_page_idx,
            end_idx_differs=row.end_page_idx != node.end_page_idx,
        )

    for row in existing:
        if row.id not in diffs:
            diffs[row.id] = HeaderDiff(
                segments=(Segment(row.header, True),),
                page_differs=row.start_page is not None,
                start_idx_differs=row.start_page_idx is not None,
                end_idx_differs=row.end_page_idx is not None,
                missing=True,
            )
    return diffs


def diff_for_document(
    db: Session, doc_id: uuid.UUID, roots: Sequence[TocNode]
) -> dict[int, HeaderDiff]:
    """``diff_against_toc`` over the document's stored headers."""
    existing = db.scalars(
        select(Header).where(Header.doc_id == doc_id).order_by(Header.id)
    ).all()
    return diff_against_toc(roots, existing)


# ---- manual editing ------------------------------------------------------------
#
# Each pane of the pop-up has its own Edit mode and its own save: the bucket
# pane rewrites the TOC inside the metadata JSON, the Stored pane rewrites the
# headers rows. The two never touch each other's side.


class RowEdit(NamedTuple):
    """One row of a pane's Edit form: the title and the three page numbers."""

    #: None means the payload carried no title for this row — leave it alone.
    title: str | None
    page: int | None
    start: int | None
    end: int | None


def parse_row_edits(raw: str) -> dict[int, RowEdit]:
    """A pane's Edit payload: row key → (title, page, start/end page_idx).

    ``raw`` is the JSON headers.js serialises, shaped like
    ``{"3": {"title": "I. 개요", "page": 5, "start": 3, "end": 7}}`` with null
    for a box left empty. The key is a header id for the Stored pane and a
    preorder seq for the bucket pane; both are ints. Anything unusable raises
    ValueError with a message fit for the dialog.
    """
    unreadable = ValueError("The edited rows could not be read.")

    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        raise unreadable from exc
    if not isinstance(data, dict):
        raise unreadable

    edits: dict[int, RowEdit] = {}
    for key, value in data.items():
        try:
            row_key = int(key)
        except (TypeError, ValueError):
            raise unreadable from None
        if not isinstance(value, dict):
            raise unreadable

        title = value.get("title")
        if title is not None:
            if not isinstance(title, str) or not title.strip():
                raise ValueError("A title cannot be empty.")
            title = title.strip()

        page = _edited_index(value.get("page"))
        start = _edited_index(value.get("start"))
        end = _edited_index(value.get("end"))
        if start is not None and end is not None and end < start:
            raise ValueError("An end index cannot be before its start.")
        edits[row_key] = RowEdit(title, page, start, end)

    return edits


def _raw_entries(entries: Any) -> Iterator[dict[str, Any]]:
    """The raw ``toc`` entries in preorder, skipping what ``_nodes`` skips.

    The same walk in the same order — an entry skipped for its blank title is
    skipped with its children, exactly as ``_nodes`` drops it — so an entry's
    position here is the ``seq`` its TocNode carries in the pop-up.
    """
    if not isinstance(entries, list):
        return
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if not str(entry.get("header") or "").strip():
            continue
        yield entry
        yield from _raw_entries(entry.get("children"))


def update_bucket_toc(document: Document, edits: dict[int, RowEdit]) -> int:
    """Overwrite the edited TOC entries in the metadata JSON; returns how many.

    Read-modify-write on the raw dict, so every field the enrichment pipeline
    put in the file — in the entries as much as at the top level — survives the
    edit. The read is validated the same way the pop-up's render was, which is
    what guarantees the seq numbering below still lines up. An edit whose seq
    the TOC no longer has is skipped rather than an error: the pop-up on
    screen can be older than the file behind it.
    """
    if not edits:
        return 0

    payload, bucket, key = _read_metadata(document)
    try:
        TocMetadata.model_validate(payload)
    except pydantic.ValidationError as exc:
        raise TransferError(f"{bucket}/{key} could not be read: {exc}") from exc

    changed = 0
    for seq, entry in enumerate(_raw_entries(payload.get("toc"))):
        edit = edits.get(seq)
        if edit is None:
            continue
        values = {
            "header": edit.title if edit.title is not None else entry.get("header"),
            "page_num": edit.page,
            "page_idx": edit.start,
            "page_idx_end": edit.end,
        }
        if any(entry.get(field) != value for field, value in values.items()):
            entry.update(values)
            changed += 1

    if changed:
        put_object(
            bucket,
            key,
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
            content_type="application/json; charset=utf-8",
        )
    return changed


class DeletedEntry(NamedTuple):
    """What deleting one TOC entry removed: its title and how many rows."""

    title: str
    removed: int


def delete_toc_entry(document: Document, seq: int) -> DeletedEntry:
    """Remove the TOC entry at preorder position ``seq`` from the metadata JSON.

    The entry goes with its sub-headings — a tree row is deleted as the tree
    row it shows. Only the ``toc`` array changes; every other field the
    pipeline wrote stays, except ``has_toc``, which turns false when the last
    entry goes. A seq the TOC no longer has is an error, not a silent no-op:
    deleting by a stale position could only ever remove the wrong heading.
    """
    payload, bucket, key = _read_metadata(document)
    try:
        TocMetadata.model_validate(payload)
    except pydantic.ValidationError as exc:
        raise TransferError(f"{bucket}/{key} could not be read: {exc}") from exc

    def visible(entry: Any) -> bool:
        return isinstance(entry, dict) and bool(str(entry.get("header") or "").strip())

    def subtree_rows(entry: dict[str, Any]) -> int:
        kids = entry.get("children")
        children = kids if isinstance(kids, list) else []
        return 1 + sum(subtree_rows(child) for child in children if visible(child))

    # The same preorder, with the same skips, as ``_raw_entries`` — which is
    # what makes the counter here agree with the seq the pop-up rendered.
    counter = count()

    def remove(entries: Any) -> DeletedEntry | None:
        if not isinstance(entries, list):
            return None
        for index, entry in enumerate(entries):
            if not visible(entry):
                continue
            if next(counter) == seq:
                del entries[index]
                return DeletedEntry(
                    title=str(entry.get("header")).strip(),
                    removed=subtree_rows(entry),
                )
            if (deleted := remove(entry.get("children"))) is not None:
                return deleted
        return None

    deleted = remove(payload.get("toc"))
    if deleted is None:
        raise TransferError(
            "That TOC entry is no longer in the metadata JSON — the file has "
            "changed since the pop-up was opened. Close and reopen it."
        )

    if not any(True for _ in _raw_entries(payload.get("toc"))):
        payload["has_toc"] = False
    put_object(
        bucket,
        key,
        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        content_type="application/json; charset=utf-8",
    )
    return deleted


def update_stored_headers(
    db: Session, doc_id: uuid.UUID, edits: dict[int, RowEdit]
) -> int:
    """Overwrite the edited headers rows; returns how many changed.

    The Stored pane's counterpart of the outline pop-up's index editor, plus
    the title. Scoped to one document, so an id belonging to another document's
    outline cannot be reached; an id that no longer exists is skipped rather
    than an error.
    """
    if not edits:
        return 0

    rows = db.scalars(
        select(Header).where(Header.doc_id == doc_id, Header.id.in_(edits))
    ).all()

    changed = 0
    for row in rows:
        edit = edits[row.id]
        values = (
            edit.title if edit.title is not None else row.header,
            edit.page,
            edit.start,
            edit.end,
        )
        if (row.header, row.start_page, row.start_page_idx, row.end_page_idx) != values:
            row.header, row.start_page, row.start_page_idx, row.end_page_idx = values
            changed += 1

    db.commit()
    return changed


def apply_toc(
    db: Session, doc_id: uuid.UUID, roots: Sequence[TocNode]
) -> TransferOutcome:
    """Write the bucket TOC into ``headers``: update matches, insert the rest.

    An updated row keeps its id — and with it every ``header_contents`` row
    already hanging off it — while its page fields, level, and place in the
    tree are taken from the TOC. Rows the TOC does not name are not touched,
    so a transfer can never silently drop a heading someone stored by hand.
    """
    existing = db.scalars(
        select(Header).where(Header.doc_id == doc_id).order_by(Header.id)
    ).all()

    updated = inserted = unchanged = 0
    touched: set[int] = set()
    # The Header row each TOC node became, looked up as its children's parent.
    row_of: dict[int, Header] = {}

    for node, parent, row in pair_with_existing(roots, existing):
        parent_row = row_of[id(parent)] if parent is not None else None
        parent_id = parent_row.id if parent_row is not None else None

        if row is None:
            row = Header(
                doc_id=doc_id,
                header=node.title,
                parent_id=parent_id,
                level=node.level,
                start_page=node.start_page,
                start_page_idx=node.start_page_idx,
                end_page_idx=node.end_page_idx,
            )
            db.add(row)
            # The id is needed as the parent of whatever nests under this
            # node, so each insert is flushed as it is added.
            db.flush()
            inserted += 1
        else:
            values = (
                parent_id,
                node.level,
                node.start_page,
                node.start_page_idx,
                node.end_page_idx,
            )
            current = (
                row.parent_id,
                row.level,
                row.start_page,
                row.start_page_idx,
                row.end_page_idx,
            )
            if current == values:
                unchanged += 1
            else:
                (
                    row.parent_id,
                    row.level,
                    row.start_page,
                    row.start_page_idx,
                    row.end_page_idx,
                ) = values
                updated += 1
            touched.add(row.id)

        row_of[id(node)] = row

    db.commit()

    return TransferOutcome(
        updated=updated,
        inserted=inserted,
        unchanged=unchanged,
        untouched=sum(1 for r in existing if r.id not in touched),
    )


def apply_rows_to_bucket(document: Document, rows: Sequence[Header]) -> int:
    """Replace the metadata JSON's TOC with the stored headers; returns how many.

    The previous ``toc`` array is discarded and rebuilt wholesale from the
    rows' tree — titles, the three page numbers, the nesting. Every entry is
    written ``matched: false``, because no pipeline located these headings in
    the content list; their numbers come from the ``headers`` table. The
    pipeline's TOC bookkeeping at the top level is reset to describe the new
    TOC (``has_toc`` true, method ``stored_headers``, no confidence, no
    warnings), while every other field it wrote stays as it was. A file the
    replacement would not change is not rewritten.
    """
    children_of: dict[int | None, list[Header]] = {}
    for row in rows:
        children_of.setdefault(row.parent_id, []).append(row)

    def build(siblings: Sequence[Header]) -> list[dict[str, Any]]:
        return [
            {
                "header": row.header,
                "page_num": row.start_page,
                "page_idx": row.start_page_idx,
                "page_idx_end": row.end_page_idx,
                "matched": False,
                "children": build(children_of.get(row.id, [])),
            }
            for row in siblings
        ]

    payload, bucket, key = _read_metadata(document)
    replacement: dict[str, Any] = {
        "toc": build(children_of.get(None, [])),
        "has_toc": True,
        "toc_method": "stored_headers",
        "toc_confidence": None,
        "toc_valid": True,
        "toc_warnings": [],
    }

    if any(payload.get(field) != value for field, value in replacement.items()):
        payload.update(replacement)
        put_object(
            bucket,
            key,
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
            content_type="application/json; charset=utf-8",
        )
    return len(rows)


def apply_headers_to_bucket(db: Session, document: Document) -> int:
    """``apply_rows_to_bucket`` over the document's stored headers."""
    rows = db.scalars(
        select(Header).where(Header.doc_id == document.id).order_by(Header.id)
    ).all()
    if not rows:
        raise TransferError(
            "No stored headers to transfer — extract or insert some first."
        )
    return apply_rows_to_bucket(document, rows)
