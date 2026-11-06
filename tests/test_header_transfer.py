"""The pure rules behind the Transfer pop-up: reading the metadata JSON's TOC
and pairing it against the stored headers.

The DB-facing apply step is a thin walk over ``pair_with_existing``; what these
tests pin is the shape of the TOC as parsed from the bucket — depth becomes
level — and the matching rules: title-key equality, duplicates claimed in
reading order, and a missing match meaning "insert".
"""

import json
import uuid
from types import SimpleNamespace

import pytest

from app.models.document import Document
from app.models.header import Header
from app.services import header_transfer
from app.services.header_transfer import (
    RowEdit,
    TransferError,
    apply_rows_to_bucket,
    delete_toc_entry,
    diff_against_toc,
    load_bucket_toc,
    metadata_key,
    pair_with_existing,
    parse_row_edits,
    title_segments,
    update_bucket_toc,
)

DOC_ID = uuid.uuid4()


def document(**overrides) -> Document:
    values = {
        "id": DOC_ID,
        "bucket": "docs",
        "s3_key": "재정간행물/해외재정동향/abc.pdf",
        "filename": "2016년_재정동향_11월_제2호.pdf",
        "size_bytes": 1,
        "content_type": "application/pdf",
    }
    values.update(overrides)
    return Document(**values)


def metadata(**overrides) -> bytes:
    payload = {
        "id": str(DOC_ID),
        "filename": "2016년 재정동향 11월 제2호.pdf",
        "pages": 30,
        "has_toc": True,
        "toc_confidence": 0.95,
        "toc_method": "keyword+pattern",
        "toc_valid": True,
        "toc_warnings": [],
        "toc": [
            {
                "header": "I. 국제 기구",
                "page_num": 2,
                "page_idx": 4,
                "page_idx_end": 8,
                "matched": True,
                "children": [
                    {
                        "header": "1. IMF",
                        "page_num": 2,
                        "page_idx": 4,
                        "page_idx_end": 6,
                        "matched": False,
                        "children": [],
                    }
                ],
            },
            {
                "header": "II. 호 주 (Australia)",
                "page_num": 7,
                "page_idx": 9,
                "page_idx_end": 9,
                "matched": False,
                "children": [],
            },
        ],
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


@pytest.fixture
def bucket(monkeypatch):
    """Point the service at an in-memory bucket of one object."""
    store: dict[str, bytes] = {}

    def read_object(bucket_name, key, *, max_bytes):
        from app.storage import ObjectNotFound

        if key not in store:
            raise ObjectNotFound(bucket_name, key)
        data = store[key]
        return (b"", True) if len(data) > max_bytes else (data, False)

    def put_object(bucket_name, key, body, *, content_type):
        store[key] = body

    monkeypatch.setattr(header_transfer, "read_object", read_object)
    monkeypatch.setattr(header_transfer, "put_object", put_object)
    monkeypatch.setattr(
        header_transfer,
        "get_settings",
        lambda: SimpleNamespace(bucket_rag_files="public-finance-rag-files"),
    )
    return store


def test_metadata_key_sits_in_the_migrated_folder() -> None:
    doc = document()
    assert metadata_key(doc) == (
        "재정간행물/해외재정동향/2016년_재정동향_11월_제2호/"
        "2016년_재정동향_11월_제2호_metadata.json"
    )


def test_load_parses_the_toc_and_levels_it_by_depth(bucket) -> None:
    doc = document()
    bucket[metadata_key(doc)] = metadata()

    toc = load_bucket_toc(doc)

    assert toc.total == 3
    root = toc.roots[0]
    assert (root.title, root.level) == ("I. 국제 기구", 1)
    assert (root.start_page, root.start_page_idx, root.end_page_idx) == (2, 4, 8)
    child = root.children[0]
    assert (child.title, child.level, child.matched) == ("1. IMF", 2, False)
    assert toc.confidence == 0.95
    assert toc.method == "keyword+pattern"


def test_load_refuses_a_missing_object(bucket) -> None:
    with pytest.raises(TransferError, match="No metadata JSON"):
        load_bucket_toc(document())


def test_load_refuses_another_documents_metadata(bucket) -> None:
    doc = document()
    bucket[metadata_key(doc)] = metadata(id=str(uuid.uuid4()))

    with pytest.raises(TransferError, match="belongs to document"):
        load_bucket_toc(doc)


def test_load_refuses_an_empty_toc(bucket) -> None:
    doc = document()
    bucket[metadata_key(doc)] = metadata(toc=[], has_toc=False)

    with pytest.raises(TransferError, match="has no TOC"):
        load_bucket_toc(doc)


# ---- pairing -----------------------------------------------------------------


def node(title: str, level: int = 1, children: tuple = ()) -> header_transfer.TocNode:
    return header_transfer.TocNode(
        title=title,
        level=level,
        start_page=None,
        start_page_idx=None,
        end_page_idx=None,
        matched=True,
        children=children,
    )


def row(row_id: int, title: str) -> Header:
    return Header(id=row_id, doc_id=DOC_ID, header=title, level=1)


def test_pairing_ignores_spacing_and_width_differences() -> None:
    # The bucket prints "II. 호 주 (Australia)"; the stored row lost the inner
    # spaces. They are the same heading.
    pairs = pair_with_existing(
        [node("II. 호 주 (Australia)")], [row(1, "II. 호주(Australia)")]
    )

    assert pairs[0][2] is not None
    assert pairs[0][2].id == 1


def test_duplicate_titles_are_claimed_in_reading_order() -> None:
    nodes = [node("1. 개요"), node("1. 개요")]
    rows = [row(1, "1. 개요"), row(2, "1. 개요")]

    matched = [match for _, _, match in pair_with_existing(nodes, rows)]

    assert [m.id for m in matched] == [1, 2]


def test_an_unmatched_node_pairs_with_none_and_keeps_its_parent() -> None:
    child = node("가. 새 절", level=2)
    parent = node("I. 장", children=(child,))

    pairs = pair_with_existing([parent], [row(1, "I. 장")])

    assert [(n.title, p.title if p else None) for n, p, _ in pairs] == [
        ("I. 장", None),
        ("가. 새 절", "I. 장"),
    ]
    assert pairs[0][2].id == 1
    assert pairs[1][2] is None


# ---- the red marks -------------------------------------------------------------


def test_identical_titles_have_no_differing_segment() -> None:
    segments = title_segments("I. 국제 기구", "I. 국제 기구")

    assert [(s.text, s.differs) for s in segments] == [("I. 국제 기구", False)]


def test_only_the_differing_characters_read_red() -> None:
    # A fullwidth numeral pairs with its plain twin (same title key) but is
    # not the same spelling; exactly that character differs, nothing else.
    segments = title_segments("I. 국제 기구", "Ⅰ. 국제 기구")

    assert [(s.text, s.differs) for s in segments] == [
        ("Ⅰ", True),
        (". 국제 기구", False),
    ]


def test_a_character_only_the_toc_has_paints_nothing() -> None:
    # "호 주" vs "호주": the stored title lacks the space, so there is nothing
    # on the stored side to mark.
    segments = title_segments("II. 호 주 (Australia)", "II. 호주 (Australia)")

    assert all(not s.differs for s in segments)
    assert "".join(s.text for s in segments) == "II. 호주 (Australia)"


def test_diff_flags_the_pills_that_disagree() -> None:
    toc_node = header_transfer.TocNode(
        title="1. 개요",
        level=1,
        start_page=3,
        start_page_idx=5,
        end_page_idx=9,
        matched=True,
    )
    stored = row(1, "1. 개요")
    stored.start_page = 3
    stored.start_page_idx = 4
    stored.end_page_idx = None

    diff = diff_against_toc([toc_node], [stored])[1]

    assert not diff.missing
    assert not diff.page_differs
    assert diff.start_idx_differs
    assert diff.end_idx_differs
    assert all(not s.differs for s in diff.segments)


def test_a_stored_header_the_toc_never_names_is_wholly_red() -> None:
    diff = diff_against_toc([node("I. 장")], [row(1, "I. 장"), row(2, "부록")])[2]

    assert diff.missing
    assert [(s.text, s.differs) for s in diff.segments] == [("부록", True)]


# ---- manual editing --------------------------------------------------------------


def test_parse_row_edits_reads_titles_and_numbers() -> None:
    edits = parse_row_edits(
        '{"3": {"title": " I. 개요 ", "page": 5, "start": 3, "end": 7},'
        ' "4": {"title": null, "page": null, "start": null, "end": null}}'
    )

    assert edits[3] == RowEdit("I. 개요", 5, 3, 7)
    assert edits[4] == RowEdit(None, None, None, None)


def test_parse_row_edits_refuses_bad_values() -> None:
    with pytest.raises(ValueError, match="title cannot be empty"):
        parse_row_edits('{"1": {"title": "  ", "page": null, "start": null, "end": null}}')
    with pytest.raises(ValueError, match="before its start"):
        parse_row_edits('{"1": {"title": null, "page": null, "start": 7, "end": 3}}')
    with pytest.raises(ValueError, match="could not be read"):
        parse_row_edits("[1, 2]")


def test_update_bucket_toc_edits_by_seq_and_keeps_pipeline_fields(bucket) -> None:
    doc = document()
    payload = json.loads(metadata())
    # Fields this reader does not know must survive the rewrite, at the top
    # level and inside an entry.
    payload["toc_llm"] = {"model": "solar-pro4"}
    payload["toc"][0]["children"][0]["custom"] = "kept"
    # A blank-header entry is skipped with its children by the pop-up, so the
    # seq numbering of the edit must skip it too.
    payload["toc"].insert(1, {"header": "  ", "children": [{"header": "ghost"}]})
    bucket[metadata_key(doc)] = json.dumps(payload, ensure_ascii=False).encode()

    # seq 1 is "1. IMF" (child of seq 0); seq 2 is "II. 호 주 …", after the blank.
    changed = update_bucket_toc(
        doc, {2: RowEdit("II. 호주 (Australia)", 7, 9, 10), 99: RowEdit(None, 1, 1, 1)}
    )

    assert changed == 1
    written = json.loads(bucket[metadata_key(doc)])
    assert written["toc_llm"] == {"model": "solar-pro4"}
    assert written["toc"][0]["children"][0]["custom"] == "kept"
    edited = written["toc"][2]
    assert edited["header"] == "II. 호주 (Australia)"
    assert (edited["page_num"], edited["page_idx"], edited["page_idx_end"]) == (7, 9, 10)
    # The untouched sibling and the blank entry are exactly as they were.
    assert written["toc"][0]["header"] == "I. 국제 기구"
    assert written["toc"][1]["header"] == "  "


def test_transfer_to_bucket_replaces_the_whole_toc(bucket) -> None:
    doc = document()
    bucket[metadata_key(doc)] = metadata()

    parent = row(1, "I. 국제 기구")
    parent.start_page, parent.start_page_idx, parent.end_page_idx = 2, 4, 9
    child = row(2, "1. IMF")
    child.parent_id = 1
    child.start_page, child.start_page_idx, child.end_page_idx = 2, 4, 6
    new = row(3, "2. OECD")
    new.parent_id = 1
    new.start_page, new.start_page_idx, new.end_page_idx = 5, 7, 8

    assert apply_rows_to_bucket(doc, [parent, child, new]) == 3

    written = json.loads(bucket[metadata_key(doc)])
    # The TOC is exactly the stored tree — the pipeline's "II. 호 주
    # (Australia)" entry is gone, and no entry claims a pipeline match.
    assert written["toc"] == [
        {
            "header": "I. 국제 기구",
            "page_num": 2,
            "page_idx": 4,
            "page_idx_end": 9,
            "matched": False,
            "children": [
                {
                    "header": "1. IMF",
                    "page_num": 2,
                    "page_idx": 4,
                    "page_idx_end": 6,
                    "matched": False,
                    "children": [],
                },
                {
                    "header": "2. OECD",
                    "page_num": 5,
                    "page_idx": 7,
                    "page_idx_end": 8,
                    "matched": False,
                    "children": [],
                },
            ],
        }
    ]
    # The TOC bookkeeping now describes the stored headers; everything else
    # the pipeline wrote is still there.
    assert written["toc_method"] == "stored_headers"
    assert written["toc_confidence"] is None
    assert (written["toc_valid"], written["toc_warnings"]) == (True, [])
    assert (written["has_toc"], written["pages"]) == (True, 30)


def test_transfer_to_bucket_builds_a_toc_where_none_was(bucket) -> None:
    doc = document()
    bucket[metadata_key(doc)] = metadata(toc=[], has_toc=False)

    parent = row(1, "I. 장")
    child = row(2, "가. 절")
    child.parent_id = 1

    assert apply_rows_to_bucket(doc, [parent, child]) == 2

    written = json.loads(bucket[metadata_key(doc)])
    assert written["has_toc"] is True
    assert written["toc"][0]["header"] == "I. 장"
    assert written["toc"][0]["children"][0]["header"] == "가. 절"


def test_transfer_to_bucket_again_writes_nothing(bucket) -> None:
    doc = document()
    bucket[metadata_key(doc)] = metadata()

    stored = row(1, "I. 국제 기구")
    stored.start_page, stored.start_page_idx, stored.end_page_idx = 2, 4, 8

    apply_rows_to_bucket(doc, [stored])
    before = bucket[metadata_key(doc)]

    assert apply_rows_to_bucket(doc, [stored]) == 1
    assert bucket[metadata_key(doc)] is before


def test_delete_removes_the_entry_with_its_subtree(bucket) -> None:
    doc = document()
    bucket[metadata_key(doc)] = metadata()

    # seq 0 is "I. 국제 기구" with the "1. IMF" child under it.
    deleted = delete_toc_entry(doc, 0)

    assert deleted.title == "I. 국제 기구"
    assert deleted.removed == 2
    written = json.loads(bucket[metadata_key(doc)])
    assert [e["header"] for e in written["toc"]] == ["II. 호 주 (Australia)"]
    assert written["has_toc"] is True


def test_delete_by_seq_skips_what_the_popup_skips(bucket) -> None:
    doc = document()
    payload = json.loads(metadata())
    payload["toc"].insert(1, {"header": " ", "children": [{"header": "ghost"}]})
    bucket[metadata_key(doc)] = json.dumps(payload, ensure_ascii=False).encode()

    # seq 2 is "II. 호 주 …", counted past the blank entry, not into it.
    deleted = delete_toc_entry(doc, 2)

    assert deleted.title == "II. 호 주 (Australia)"
    written = json.loads(bucket[metadata_key(doc)])
    assert [e["header"] for e in written["toc"]] == ["I. 국제 기구", " "]


def test_deleting_the_last_entry_turns_has_toc_off(bucket) -> None:
    doc = document()
    toc = [{"header": "only one", "page_num": 1, "page_idx": 1, "page_idx_end": 2}]
    bucket[metadata_key(doc)] = metadata(toc=toc)

    deleted = delete_toc_entry(doc, 0)

    assert deleted.removed == 1
    written = json.loads(bucket[metadata_key(doc)])
    assert written["toc"] == []
    assert written["has_toc"] is False
    assert written["toc_confidence"] == 0.95  # the pipeline's fields stay


def test_delete_refuses_a_stale_seq(bucket) -> None:
    doc = document()
    before = bucket[metadata_key(doc)] = metadata()

    with pytest.raises(TransferError, match="no longer in the metadata JSON"):
        delete_toc_entry(doc, 99)
    assert bucket[metadata_key(doc)] is before


def test_update_bucket_toc_without_changes_writes_nothing(bucket) -> None:
    doc = document()
    bucket[metadata_key(doc)] = metadata()
    before = bucket[metadata_key(doc)]

    # The same values the entry already has.
    changed = update_bucket_toc(doc, {0: RowEdit("I. 국제 기구", 2, 4, 8)})

    assert changed == 0
    assert bucket[metadata_key(doc)] is before
