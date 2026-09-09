"""The deterministic table HTML → Markdown conversion."""

import pytest

from app.services.html_table import (
    TableParseError,
    build_grid,
    convert_html_table,
    detect_header_rows,
    flatten_headers,
    format_caption,
    format_footnotes,
    html_table_to_markdown,
    parse_html_table,
)


def rows_of(markdown: str) -> list[str]:
    """The table lines of a Markdown text, caption and footnotes stripped."""
    return [line for line in markdown.split("\n") if line.startswith("|")]


# ---- plain tables ------------------------------------------------------------


def test_simple_table_with_th_header() -> None:
    html = (
        "<table><tr><th>Name</th><th>Value</th></tr>"
        "<tr><td>a</td><td>1</td></tr><tr><td>b</td><td>2</td></tr></table>"
    )
    assert html_table_to_markdown(html) == (
        "| Name | Value |\n| --- | --- |\n| a | 1 |\n| b | 2 |"
    )


def test_table_without_thead_or_th_detects_year_header() -> None:
    html = (
        "<table><tr><td></td><td>2012</td><td>2013</td></tr>"
        "<tr><td>세계</td><td>-3.8</td><td>-2.9</td></tr></table>"
    )
    assert rows_of(html_table_to_markdown(html)) == [
        "|  | 2012 | 2013 |",
        "| --- | --- | --- |",
        "| 세계 | -3.8 | -2.9 |",
    ]


def test_table_with_thead_uses_every_thead_row() -> None:
    html = (
        "<table><thead><tr><td>A</td><td>B</td></tr><tr><td>a1</td><td>b1</td></tr></thead>"
        "<tbody><tr><td>1</td><td>2</td></tr></tbody></table>"
    )
    assert rows_of(html_table_to_markdown(html))[0] == "| A / a1 | B / b1 |"


def test_table_with_no_header_gets_an_empty_header_row() -> None:
    """A continuation table with values from the first row: nothing is
    promoted to a header, and no header is invented."""
    html = (
        "<table><tr><td>Advanced Economies</td><td>1.4</td><td>0.3</td></tr>"
        "<tr><td>Emerging</td><td>4.7</td><td>4.7</td></tr></table>"
    )
    assert rows_of(html_table_to_markdown(html)) == [
        "|  |  |  |",
        "| --- | --- | --- |",
        "| Advanced Economies | 1.4 | 0.3 |",
        "| Emerging | 4.7 | 4.7 |",
    ]


# ---- spans -------------------------------------------------------------------


def test_colspan_header_is_flattened_over_its_columns() -> None:
    html = (
        "<table><tr><td></td><td colspan='3'>Actual Data</td><td colspan='2'>Projection</td></tr>"
        "<tr><td></td><td>2013</td><td>2014</td><td>2015</td><td>2016</td><td>2017</td></tr>"
        "<tr><td>Asia</td><td>5.7</td><td>5.6</td><td>5.4</td><td>5.3</td><td>5.3</td></tr></table>"
    )
    assert rows_of(html_table_to_markdown(html))[0] == (
        "|  | Actual Data / 2013 | Actual Data / 2014 | Actual Data / 2015 "
        "| Projection / 2016 | Projection / 2017 |"
    )


def test_rowspan_header_cell_counts_once() -> None:
    html = (
        "<table><tr><td rowspan='2'>Country</td><td>2016</td></tr>"
        "<tr><td>Q1</td></tr>"
        "<tr><td>Korea</td><td>2.7</td></tr></table>"
    )
    lines = rows_of(html_table_to_markdown(html))
    assert lines[0] == "| Country | 2016 / Q1 |"
    assert lines[2] == "| Korea | 2.7 |"


def test_rowspan_and_colspan_together() -> None:
    html = (
        "<table>"
        "<tr><td rowspan='2' colspan='2'>Item</td><td colspan='2'>Group</td></tr>"
        "<tr><td>x</td><td>y</td></tr>"
        "<tr><td>a</td><td>b</td><td>1</td><td>2</td></tr>"
        "</table>"
    )
    grid = build_grid(parse_html_table(html))
    assert grid.width == 4 and grid.height == 3
    assert detect_header_rows(grid) == 2
    assert flatten_headers(grid, 2) == ["Item", "Item", "Group / x", "Group / y"]
    assert rows_of(html_table_to_markdown(html))[2] == "| a | b | 1 | 2 |"


def test_rowspan_in_body_repeats_label_and_colspan_does_not_repeat_value() -> None:
    html = (
        "<table><tr><td>Area</td><td>A</td><td>B</td></tr>"
        "<tr><td rowspan='2'>North</td><td>1</td><td>2</td></tr>"
        "<tr><td colspan='2'>3</td></tr></table>"
    )
    assert rows_of(html_table_to_markdown(html))[2:] == [
        "| North | 1 | 2 |",
        "| North | 3 |  |",
    ]


def test_grid_expansion_places_every_cell() -> None:
    html = (
        "<table><tr><td rowspan='2'>a</td><td>b</td><td>c</td></tr>"
        "<tr><td>d</td><td>e</td></tr></table>"
    )
    grid = build_grid(parse_html_table(html))
    texts = [[cell.text if cell else None for cell in row] for row in grid.cells]
    assert texts == [["a", "b", "c"], ["a", "d", "e"]]


# ---- what is kept exactly ----------------------------------------------------


def test_empty_first_header_cell_stays_empty() -> None:
    html = "<table><tr><td></td><td>2016</td></tr><tr><td>Japan</td><td>0.5</td></tr></table>"
    assert rows_of(html_table_to_markdown(html))[0] == "|  | 2016 |"


def test_empty_data_cells_stay_empty() -> None:
    html = (
        "<table><tr><td>Area</td><td>단기</td><td>중기</td></tr>"
        "<tr><td>고용보호법제</td><td></td><td>-</td></tr></table>"
    )
    assert rows_of(html_table_to_markdown(html))[2] == "| 고용보호법제 |  | - |"


def test_negative_numbers_and_precision_are_untouched() -> None:
    html = (
        "<table><tr><td></td><td>2016</td><td>2017</td><td>2018</td></tr>"
        "<tr><td>Japan</td><td>-0.1</td><td>0.0</td><td>5.70</td></tr></table>"
    )
    assert rows_of(html_table_to_markdown(html))[2] == "| Japan | -0.1 | 0.0 | 5.70 |"


def test_section_row_is_kept_with_empty_cells() -> None:
    html = (
        "<table><tr><td></td><td>2016</td><td>2017</td></tr>"
        "<tr><td>Asia</td><td>5.3</td><td>5.3</td></tr>"
        "<tr><td colspan='3'>Memorandum</td></tr>"
        "<tr><td>World</td><td>3.2</td><td>3.5</td></tr></table>"
    )
    assert rows_of(html_table_to_markdown(html))[3] == "| Memorandum |  |  |"


def test_pipe_in_a_cell_is_escaped() -> None:
    html = "<table><tr><td>Name</td><td>Value</td></tr><tr><td>a | b</td><td>1</td></tr></table>"
    assert rows_of(html_table_to_markdown(html))[2] == "| a \\| b | 1 |"


def test_entities_and_line_breaks_inside_cells() -> None:
    html = "<table><tr><td>Name</td><td>Note</td></tr><tr><td>R&amp;D</td><td>one<br>two</td></tr></table>"
    assert rows_of(html_table_to_markdown(html))[2] == "| R&D | one two |"


# ---- caption and footnotes ---------------------------------------------------


def test_caption_lines_are_joined_and_bold() -> None:
    assert format_caption(["<표 I-1> 세계 경제성장률 전망", "(단위: %)"]) == (
        "**<표 I-1> 세계 경제성장률 전망 (단위: %)**"
    )
    assert format_caption([]) is None
    assert format_caption(None) is None


def test_footnotes_keep_order_including_the_source_line() -> None:
    notes = [
        "주:1)신흥아시아 국가는 중국을 포함",
        "2)소규모 국가는 부탄 등을 포함",
        "출처: IMF, Regional Economic Outlook April 2016, Table 1.1.",
    ]
    assert format_footnotes(notes) == (
        "### Footnotes\n\n"
        "- 주:1)신흥아시아 국가는 중국을 포함\n"
        "- 2)소규모 국가는 부탄 등을 포함\n"
        "- 출처: IMF, Regional Economic Outlook April 2016, Table 1.1."
    )
    assert format_footnotes([]) is None


def test_no_caption_and_no_footnotes_means_only_the_table() -> None:
    html = "<table><tr><td>A</td><td>B</td></tr><tr><td>1</td><td>2</td></tr></table>"
    text = html_table_to_markdown(html, caption=[], footnotes=[])
    assert text.startswith("| A | B |")
    assert "Footnotes" not in text and "**" not in text


# ---- failures ----------------------------------------------------------------


@pytest.mark.parametrize(
    "html",
    [
        "",
        "   ",
        "<table></table>",
        "<p>no table here</p>",
        "<table><td>cell outside a row</td></table>",
        "<table><tr><td><table><tr><td>nested</td></tr></table></td></tr></table>",
        "<table><tr><td colspan='99999'>x</td></tr></table>",
    ],
)
def test_malformed_html_raises(html: str) -> None:
    with pytest.raises(TableParseError):
        html_table_to_markdown(html)


def test_unclosed_tags_are_still_read() -> None:
    """The lenient parser closes what the writer forgot; no cell is lost."""
    html = "<table><tr><td>A<td>B<tr><td>1<td>2</table>"
    assert rows_of(html_table_to_markdown(html)) == [
        "| A | B |",
        "| --- | --- |",
        "| 1 | 2 |",
    ]


def test_short_rows_are_padded_not_dropped() -> None:
    html = "<table><tr><td>A</td><td>B</td><td>C</td></tr><tr><td>1</td></tr></table>"
    assert rows_of(html_table_to_markdown(html))[2] == "| 1 |  |  |"


# ---- the shape this corpus produces ------------------------------------------

IMF_TABLE = (
    "<table>"
    '<tr><td rowspan="2"></td><td colspan="2">Actual Data</td><td colspan="3">Projection</td>'
    '<td colspan="3">Difference from 2015.10 WEO</td></tr>'
    "<tr><td>2013</td><td>2014</td><td>2015</td><td>2016</td><td>2017</td><td>2015</td><td>2016</td><td>2017</td></tr>"
    "<tr><td>Asia</td><td>5.7</td><td>5.6</td><td>5.4</td><td>5.3</td><td>5.3</td><td>0.0</td><td>-0.1</td><td>-0.1</td></tr>"
    "<tr><td>Emerging  $Asia^{1)}$ </td><td>7.0</td><td>6.8</td><td>6.6</td><td>6.4</td><td>6.3</td><td>0.1</td><td>0.1</td><td>0.1</td></tr>"
    "<tr><td>Macao SAR</td><td>11.2</td><td>-0.9</td><td>-20.3</td><td>-7.2</td><td>0.7</td><td>...</td><td>...</td><td>...</td></tr>"
    '<tr><td colspan="9">Memorandum</td></tr>'
    "<tr><td>World</td><td>3.3</td><td>3.4</td><td>3.1</td><td>3.2</td><td>3.5</td><td>0.0</td><td>-0.4</td><td>-0.3</td></tr>"
    "</table>"
)

IMF_CAPTION = ["<표 I-1> 세계 경제성장률 전망", "(단위: %)"]
IMF_FOOTNOTES = [
    "주:1)신흥아시아 국가는 중국,인도,인도네시아,말레이시아,필리핀,타이,베트남을 포함",
    "2)소규모 국가는 부탄, 피지, 키리바시, 몰디브, 마셀제도, 마이크로네시아 등을 포함",
    "출처: IMF, Regional Economic Outlook April 2016, Table 1.1.",
]


def test_imf_style_table_end_to_end() -> None:
    result = convert_html_table(IMF_TABLE, IMF_CAPTION, IMF_FOOTNOTES)
    assert result.header_rows == 2
    assert result.columns == 9
    assert result.rows == 5
    assert result.markdown == "\n".join(
        [
            "**<표 I-1> 세계 경제성장률 전망 (단위: %)**",
            "",
            "|  | Actual Data / 2013 | Actual Data / 2014 | Projection / 2015 | Projection / 2016 "
            "| Projection / 2017 | Difference from 2015.10 WEO / 2015 "
            "| Difference from 2015.10 WEO / 2016 | Difference from 2015.10 WEO / 2017 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
            "| Asia | 5.7 | 5.6 | 5.4 | 5.3 | 5.3 | 0.0 | -0.1 | -0.1 |",
            "| Emerging $Asia^{1)}$ | 7.0 | 6.8 | 6.6 | 6.4 | 6.3 | 0.1 | 0.1 | 0.1 |",
            "| Macao SAR | 11.2 | -0.9 | -20.3 | -7.2 | 0.7 | ... | ... | ... |",
            "| Memorandum |  |  |  |  |  |  |  |  |",
            "| World | 3.3 | 3.4 | 3.1 | 3.2 | 3.5 | 0.0 | -0.4 | -0.3 |",
            "",
            "### Footnotes",
            "",
            "- 주:1)신흥아시아 국가는 중국,인도,인도네시아,말레이시아,필리핀,타이,베트남을 포함",
            "- 2)소규모 국가는 부탄, 피지, 키리바시, 몰디브, 마셀제도, 마이크로네시아 등을 포함",
            "- 출처: IMF, Regional Economic Outlook April 2016, Table 1.1.",
        ]
    )


def test_header_then_full_width_section_row() -> None:
    """A year header followed straight away by a section row: the section
    row is not swallowed into the header."""
    html = (
        "<table><tr><td></td><td>2012</td><td>2013</td></tr>"
        '<tr><td colspan="3">재정수지</td></tr>'
        "<tr><td>세계</td><td>-3.8</td><td>-2.9</td></tr></table>"
    )
    assert rows_of(html_table_to_markdown(html)) == [
        "|  | 2012 | 2013 |",
        "| --- | --- | --- |",
        "| 재정수지 |  |  |",
        "| 세계 | -3.8 | -2.9 |",
    ]


def test_two_row_header_does_not_extend_into_symbol_data() -> None:
    """Rows of '+' and '-' are data, not a third header level."""
    html = (
        '<table><tr><td rowspan="2">개혁분야</td><td colspan="2">정상</td></tr>'
        "<tr><td>단기</td><td>중기</td></tr>"
        "<tr><td>상품시장</td><td>+</td><td>++</td></tr></table>"
    )
    lines = rows_of(html_table_to_markdown(html))
    assert lines[0] == "| 개혁분야 | 정상 / 단기 | 정상 / 중기 |"
    assert lines[2] == "| 상품시장 | + | ++ |"
