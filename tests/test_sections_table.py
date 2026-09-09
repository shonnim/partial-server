"""The table branch of the Section run: block in, header_contents row out."""

import pytest

from app.services.html_table import TableParseError
from app.services.sections import _row_for, _Tally

BLOCK = {
    "type": "table",
    "table_caption": ["<표 I-1> 세계 경제성장률 전망", "(단위: %)"],
    "table_footnote": ["출처: IMF, Regional Economic Outlook April 2016, Table 1.1."],
    "table_body": (
        "<table><tr><td></td><td>2016</td><td>2017</td></tr>"
        "<tr><td>Asia</td><td>5.3</td><td>5.3</td></tr></table>"
    ),
    "bbox": [142, 174, 858, 866],
    "page_idx": 7,
}


def test_table_block_becomes_a_markdown_row() -> None:
    tally = _Tally()
    row = _row_for(None, None, BLOCK, tally)

    assert row is not None
    assert row.type == "table"
    assert row.text == "\n".join(
        [
            "**<표 I-1> 세계 경제성장률 전망 (단위: %)**",
            "",
            "|  | 2016 | 2017 |",
            "| --- | --- | --- |",
            "| Asia | 5.3 | 5.3 |",
            "",
            "### Footnotes",
            "",
            "- 출처: IMF, Regional Economic Outlook April 2016, Table 1.1.",
        ]
    )
    assert row.caption == "<표 I-1> 세계 경제성장률 전망\n(단위: %)"
    assert row.structured_json_table is None
    assert row.page_idx == 7
    assert (row.bbox_0, row.bbox_1, row.bbox_2, row.bbox_3) == (142, 174, 858, 866)
    assert tally.tables == 1
    assert tally.llm_seconds == 0.0


def test_table_without_body_keeps_caption_and_footnotes_only() -> None:
    tally = _Tally()
    row = _row_for(None, None, {**BLOCK, "table_body": ""}, tally)

    assert row is not None
    assert row.text == "출처: IMF, Regional Economic Outlook April 2016, Table 1.1."
    assert row.caption == "<표 I-1> 세계 경제성장률 전망\n(단위: %)"
    assert tally.tables_without_body == 1
    assert tally.tables == 0


def test_unreadable_table_raises_rather_than_storing() -> None:
    with pytest.raises(TableParseError):
        _row_for(None, None, {**BLOCK, "table_body": "<p>not a table</p>"}, _Tally())
