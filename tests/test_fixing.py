"""The spread-splitting rules behind the Fixing page.

The storage- and DB-facing batch is exercised separately; these tests pin the
pure rules — above all that a landscape sheet becomes its left and right
halves in reading order, and that a document with no spreads is left alone.
"""

import uuid
from io import BytesIO

import pytest
from pypdf import PdfReader, PdfWriter

from app.models.document import Document
from app.services.fixing import (
    UnreadablePdf,
    analyze_pdf,
    backup_key_for,
    unspread_pdf,
)

# ISO A4 in points, portrait and landscape (a landscape sheet of two A4
# portrait pages has the same box as a plain landscape A4 — only bigger).
PORTRAIT = (595.0, 842.0)
LANDSCAPE = (842.0, 595.0)


def pdf_bytes(sizes: list[tuple[float, float]], rotations: list[int] | None = None) -> bytes:
    writer = PdfWriter()
    for index, (width, height) in enumerate(sizes):
        page = writer.add_blank_page(width=width, height=height)
        if rotations and rotations[index]:
            page.rotate(rotations[index])
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def test_every_sheet_two_up_is_split_in_reading_order() -> None:
    fixed, analysis = unspread_pdf(pdf_bytes([LANDSCAPE] * 3))

    assert analysis.sheets == 3
    assert analysis.spreads == 3
    assert analysis.result_pages == 6
    assert fixed is not None

    pages = PdfReader(BytesIO(fixed)).pages
    assert len(pages) == 6
    mid = LANDSCAPE[0] / 2
    # Left half first, then right, for every sheet.
    for left, right in zip(pages[0::2], pages[1::2], strict=True):
        assert float(left.mediabox.left) == pytest.approx(0)
        assert float(left.mediabox.right) == pytest.approx(mid)
        assert float(right.mediabox.left) == pytest.approx(mid)
        assert float(right.mediabox.right) == pytest.approx(LANDSCAPE[0])
        assert float(left.mediabox.height) == pytest.approx(LANDSCAPE[1])


def test_single_cover_passes_through_and_the_rest_is_split() -> None:
    fixed, analysis = unspread_pdf(pdf_bytes([PORTRAIT, LANDSCAPE, LANDSCAPE]))

    assert analysis.sheets == 3
    assert analysis.spreads == 2
    assert fixed is not None

    pages = PdfReader(BytesIO(fixed)).pages
    assert len(pages) == 5
    assert float(pages[0].mediabox.width) == pytest.approx(PORTRAIT[0])
    assert float(pages[1].mediabox.width) == pytest.approx(LANDSCAPE[0] / 2)


def test_a_document_with_no_spreads_is_left_alone() -> None:
    data = pdf_bytes([PORTRAIT] * 4)

    fixed, analysis = unspread_pdf(data)

    assert fixed is None
    assert analysis.spreads == 0
    assert not analysis.needs_fix
    assert analyze_pdf(data).sheets == 4


def test_minority_landscape_sheets_are_wide_tables_not_spreads() -> None:
    # A portrait report with a couple of landscape table pages must be left
    # whole — cutting a wide table in half would be far worse than parsing it
    # sideways.
    data = pdf_bytes([PORTRAIT] * 9 + [LANDSCAPE])

    fixed, analysis = unspread_pdf(data)

    assert fixed is None
    assert analysis.spreads == 1
    assert analysis.minority_landscape
    assert not analysis.needs_fix


def test_near_square_sheets_are_not_treated_as_spreads() -> None:
    # Just under the ratio margin: a squarish cover must not be cut in half.
    analysis = analyze_pdf(pdf_bytes([(600.0, 595.0)]))
    assert analysis.spreads == 0


def test_rotated_spread_splits_along_the_displayed_axis() -> None:
    # A portrait media box displayed landscape via /Rotate 90: the visual
    # left half is the bottom half of the box, and the rotation is kept.
    fixed, analysis = unspread_pdf(pdf_bytes([PORTRAIT], rotations=[90]))

    assert analysis.spreads == 1
    assert fixed is not None

    pages = PdfReader(BytesIO(fixed)).pages
    assert len(pages) == 2
    mid = PORTRAIT[1] / 2
    assert float(pages[0].mediabox.bottom) == pytest.approx(0)
    assert float(pages[0].mediabox.top) == pytest.approx(mid)
    assert float(pages[1].mediabox.bottom) == pytest.approx(mid)
    assert all(page.rotation % 360 == 90 for page in pages)


def test_unreadable_bytes_surface_as_unreadable_pdf() -> None:
    with pytest.raises(UnreadablePdf):
        analyze_pdf(b"this is not a pdf")


def test_backup_key_sits_beside_the_source() -> None:
    document = Document(s3_key="folder/abc-123.pdf")
    assert backup_key_for(document) == "folder/abc-123_original.pdf"


def test_check_pages_corrects_rows_that_disagree_with_the_pdf(monkeypatch) -> None:
    """Check pages trusts the PDF: a row whose stored count differs is set to
    the real count and committed, a matching row is left alone, and an
    unreadable PDF is recorded per document without stopping the batch."""
    from app.services import fixing

    doc_ok = Document(
        id=uuid.uuid4(), bucket="documents", s3_key="docs/ok.pdf",
        filename="ok.pdf", pages=3,
    )
    doc_wrong = Document(
        id=uuid.uuid4(), bucket="documents", s3_key="docs/wrong.pdf",
        filename="wrong.pdf", pages=7,
    )
    doc_bad = Document(
        id=uuid.uuid4(), bucket="documents", s3_key="docs/bad.pdf",
        filename="bad.pdf", pages=2,
    )

    store = {
        "docs/ok.pdf": pdf_bytes([PORTRAIT] * 3),
        "docs/wrong.pdf": pdf_bytes([PORTRAIT] * 3),
        "docs/bad.pdf": b"this is not a pdf",
    }
    monkeypatch.setattr(
        fixing, "read_object", lambda bucket, key, *, max_bytes: (store[key], False)
    )
    documents = {d.id: d for d in (doc_ok, doc_wrong, doc_bad)}
    monkeypatch.setattr(fixing, "_documents_by_id", lambda db, ids: documents)

    class FakeSession:
        commits = 0

        def commit(self) -> None:
            self.commits += 1
            type(self).commits += 1

        def rollback(self) -> None:
            pass

    db = FakeSession()
    result = fixing.check_pages(db, [doc_ok.id, doc_wrong.id, doc_bad.id])

    assert result.matched == 1
    assert result.corrected == 1
    assert result.failed == 1

    by_id = {c.doc_id: c for c in result.checks}
    assert by_id[doc_ok.id].ok and not by_id[doc_ok.id].corrected
    assert by_id[doc_wrong.id].corrected
    assert by_id[doc_wrong.id].db_pages == 7  # the value before the correction
    assert by_id[doc_wrong.id].real_pages == 3
    assert not by_id[doc_bad.id].ok

    # The row now matches the PDF, and only the corrected row was committed.
    assert doc_wrong.pages == 3
    assert doc_ok.pages == 3
    assert doc_bad.pages == 2  # untouched on error
    assert FakeSession.commits == 1
    assert result.corrections == [by_id[doc_wrong.id]]


def test_fix_backs_up_the_source_then_replaces_it_in_place(monkeypatch) -> None:
    """The whole single-document flow, over an in-memory object store.

    The fixed PDF must land under the document's own key — that is what every
    downstream reader uses — with the previous bytes moved aside first, and a
    re-fix must change nothing: the key already holds single pages, and the
    backup must keep the true original.
    """
    from app.services import fixing

    source = pdf_bytes([LANDSCAPE] * 2)
    store: dict[str, bytes] = {"docs/abc.pdf": source}
    copies: list[tuple[str, str]] = []

    monkeypatch.setattr(
        fixing, "read_object", lambda bucket, key, *, max_bytes: (store[key], False)
    )
    monkeypatch.setattr(
        fixing,
        "put_object",
        lambda bucket, key, body, *, content_type: store.__setitem__(key, body),
    )

    def fake_copy(src_bucket: str, src_key: str, dst_bucket: str, dst_key: str) -> None:
        copies.append((src_key, dst_key))
        store[dst_key] = store[src_key]

    monkeypatch.setattr(fixing, "copy_object", fake_copy)

    document = Document(
        id=uuid.uuid4(), bucket="documents", s3_key="docs/abc.pdf", filename="abc.pdf"
    )
    fix = fixing._fix_one(document)

    assert fix.wrote
    assert fix.key == "docs/abc.pdf"  # the document's key, not a new one
    assert store["docs/abc_original.pdf"] == source  # backup holds the source
    assert store["docs/abc.pdf"] != source  # the key now holds the fixed PDF
    assert len(PdfReader(BytesIO(store["docs/abc.pdf"])).pages) == 4
    assert document.s3_key == "docs/abc.pdf"
    assert document.original_s3_key == "docs/abc_original.pdf"
    # The row reflects the PDF it now points at: the new page count and size.
    assert document.pages == 4
    assert document.size_bytes == len(store["docs/abc.pdf"])

    # Re-fixing finds no spreads: nothing is written and — above all — the
    # backup is not overwritten with the already-fixed bytes.
    again = fixing._fix_one(document)
    assert again.ok and not again.wrote
    assert copies == [("docs/abc.pdf", "docs/abc_original.pdf")]
    assert store["docs/abc_original.pdf"] == source
