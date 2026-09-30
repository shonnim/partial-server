"""The table branch of the Section run: block in, header_contents row out."""

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


def test_table_block_keeps_the_html_body_as_it_came() -> None:
    tally = _Tally()
    row = _row_for(None, None, BLOCK, tally)

    assert row is not None
    assert row.type == "table"
    assert row.content == BLOCK["table_body"]
    assert row.caption == "<표 I-1> 세계 경제성장률 전망\n(단위: %)"
    assert row.footnote == "출처: IMF, Regional Economic Outlook April 2016, Table 1.1."
    assert row.page_idx == 7
    assert (row.bbox_0, row.bbox_1, row.bbox_2, row.bbox_3) == (142, 174, 858, 866)
    assert tally.tables == 1
    assert tally.llm_seconds == 0.0


def test_table_without_body_keeps_caption_and_footnotes_only() -> None:
    tally = _Tally()
    row = _row_for(None, None, {**BLOCK, "table_body": ""}, tally)

    assert row is not None
    assert row.content is None
    assert row.caption == "<표 I-1> 세계 경제성장률 전망\n(단위: %)"
    assert row.footnote == "출처: IMF, Regional Economic Outlook April 2016, Table 1.1."
    assert tally.tables_without_body == 1
    assert tally.tables == 0


def test_html_the_converter_would_reject_is_still_stored() -> None:
    # Nothing reads the HTML at this stage, so a body that is not a table is
    # not the Section run's problem: it is stored as it came.
    tally = _Tally()
    row = _row_for(None, None, {**BLOCK, "table_body": "<p>not a table</p>"}, tally)

    assert row is not None
    assert row.content == "<p>not a table</p>"
    assert tally.tables == 1


def test_chart_block_splits_content_caption_and_footnote() -> None:
    tally = _Tally()
    row = _row_for(
        None,
        None,
        {
            "type": "chart",
            "content": "2016년 성장률은 2.8%로 전망된다.",
            "chart_caption": ["[그림 1] 성장률 추이", "(단위: %)"],
            "chart_footnote": ["주: 한국은행 추정치.", "출처: 한국은행."],
            "page_idx": 3,
        },
        tally,
    )

    assert row is not None
    assert row.content == "2016년 성장률은 2.8%로 전망된다."
    assert row.caption == "[그림 1] 성장률 추이\n(단위: %)"
    assert row.footnote == "주: 한국은행 추정치.\n출처: 한국은행."
    assert tally.charts == 1
