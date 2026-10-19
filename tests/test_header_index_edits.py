"""Hand-editing a document's stored page numbers and indexes from the outline pop-up."""

import uuid

import pytest

from app.models.header import Header
from app.services.header_extraction import (
    IndexEdit,
    parse_index_edits,
    update_page_indexes,
)

DOC_ID = uuid.uuid4()


# ---- parsing the Save button's payload --------------------------------------


def test_parse_reads_ids_and_all_three_fields() -> None:
    raw = (
        '{"12": {"page": 5, "start": 3, "end": 7},'
        ' "13": {"page": null, "start": null, "end": null}}'
    )

    assert parse_index_edits(raw) == {
        12: IndexEdit(5, 3, 7),
        13: IndexEdit(None, None, None),
    }


def test_parse_without_a_page_key_marks_the_page_untouched() -> None:
    # A browser still running the serialiser from before the page box existed
    # sends no "page" key; that must not read as "clear the stored page".
    assert parse_index_edits('{"12": {"start": 3, "end": 7}}') == {
        12: IndexEdit(None, 3, 7, touch_page=False)
    }


def test_parse_empty_payload_is_no_edits() -> None:
    assert parse_index_edits("") == {}
    assert parse_index_edits("{}") == {}


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[1, 2]",
        '{"abc": {"start": 1, "end": 2}}',
        '{"12": [1, 2]}',
    ],
)
def test_parse_rejects_an_unreadable_payload(raw: str) -> None:
    with pytest.raises(ValueError, match="could not be read"):
        parse_index_edits(raw)


@pytest.mark.parametrize(
    "raw",
    [
        '{"12": {"start": -1, "end": 2}}',
        '{"12": {"page": -3, "start": 1, "end": 2}}',
    ],
)
def test_parse_rejects_a_negative_number(raw: str) -> None:
    with pytest.raises(ValueError, match="negative"):
        parse_index_edits(raw)


@pytest.mark.parametrize(
    "raw",
    [
        '{"12": {"start": 1.5, "end": 2}}',
        '{"12": {"page": 2.5, "start": 1, "end": 2}}',
    ],
)
def test_parse_rejects_a_fractional_number(raw: str) -> None:
    with pytest.raises(ValueError, match="whole number"):
        parse_index_edits(raw)


def test_parse_rejects_an_end_before_its_start() -> None:
    with pytest.raises(ValueError, match="before its start"):
        parse_index_edits('{"12": {"start": 9, "end": 2}}')


def test_parse_allows_one_known_end() -> None:
    assert parse_index_edits('{"12": {"start": 4, "end": null}}') == {
        12: IndexEdit(None, 4, None, touch_page=False)
    }


def test_parse_an_explicit_null_page_reads_as_cleared() -> None:
    assert parse_index_edits('{"12": {"page": null, "start": 4, "end": 5}}') == {
        12: IndexEdit(None, 4, 5)
    }


# ---- applying the edits ------------------------------------------------------


class StubScalars:
    def __init__(self, rows: list[Header]) -> None:
        self.rows = rows

    def all(self) -> list[Header]:
        return self.rows


class StubDb:
    """Stands in for the Session: one headers query and the final commit."""

    def __init__(self, rows: list[Header]) -> None:
        self.rows = rows
        self.committed = False

    def scalars(self, statement: object) -> StubScalars:
        return StubScalars(self.rows)

    def commit(self) -> None:
        self.committed = True


def make_header(
    header_id: int, start: int | None, end: int | None, page: int | None = None
) -> Header:
    return Header(
        id=header_id,
        doc_id=DOC_ID,
        header=f"header {header_id}",
        level=1,
        start_page=page,
        start_page_idx=start,
        end_page_idx=end,
    )


def test_update_overwrites_and_counts_only_changed_rows() -> None:
    unchanged = make_header(2, 8, 9, page=4)
    edited = make_header(1, 2, 3, page=1)
    db = StubDb([edited, unchanged])

    changed = update_page_indexes(
        db, DOC_ID, {1: IndexEdit(2, 5, 6), 2: IndexEdit(4, 8, 9)}
    )

    assert changed == 1
    assert (edited.start_page, edited.start_page_idx, edited.end_page_idx) == (2, 5, 6)
    assert (
        unchanged.start_page,
        unchanged.start_page_idx,
        unchanged.end_page_idx,
    ) == (4, 8, 9)
    assert db.committed


def test_update_overwrites_the_printed_page_alone() -> None:
    row = make_header(1, 2, 3, page=7)
    db = StubDb([row])

    assert update_page_indexes(db, DOC_ID, {1: IndexEdit(9, 2, 3)}) == 1
    assert (row.start_page, row.start_page_idx, row.end_page_idx) == (9, 2, 3)


def test_update_from_a_stale_payload_keeps_the_stored_page() -> None:
    row = make_header(1, 2, 3, page=7)
    db = StubDb([row])

    changed = update_page_indexes(
        db, DOC_ID, {1: IndexEdit(None, 5, 6, touch_page=False)}
    )

    assert changed == 1
    assert (row.start_page, row.start_page_idx, row.end_page_idx) == (7, 5, 6)


def test_update_can_clear_the_page_with_an_explicit_null() -> None:
    row = make_header(1, 2, 3, page=7)
    db = StubDb([row])

    assert update_page_indexes(db, DOC_ID, {1: IndexEdit(None, 2, 3)}) == 1
    assert (row.start_page, row.start_page_idx, row.end_page_idx) == (None, 2, 3)


def test_update_can_clear_an_index_to_null() -> None:
    row = make_header(1, 2, 3, page=7)
    db = StubDb([row])

    assert update_page_indexes(db, DOC_ID, {1: IndexEdit(7, 2, None)}) == 1
    assert (row.start_page, row.start_page_idx, row.end_page_idx) == (7, 2, None)


def test_update_with_no_edits_touches_nothing() -> None:
    db = StubDb([])

    assert update_page_indexes(db, DOC_ID, {}) == 0
    assert not db.committed
