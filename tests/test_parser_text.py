r"""Taking the parser's markdown escaping off a block's text."""

from app.services.parser_text import BULLET, unescape
from app.services.sections import _row_for, _Tally


def test_a_line_opening_with_an_escaped_dash_is_the_bullet() -> None:
    assert unescape(r"\- 수도권 0.1% 상승") == f"{BULLET} 수도권 0.1% 상승"


def test_the_bullet_is_restored_on_every_line_of_a_block() -> None:
    assert unescape("A\n  \\- B") == f"A\n  {BULLET} B"


def test_a_dash_inside_a_sentence_stays_a_dash() -> None:
    assert unescape(r"케이스\-칠러 가격지수") == "케이스-칠러 가격지수"
    assert unescape(r"\-5.4%") == "-5.4%"


def test_escaped_markdown_punctuation_loses_its_backslash() -> None:
    assert unescape(r"\* 두바이유(달러/배럴): 52.0") == "* 두바이유(달러/배럴): 52.0"
    assert unescape(r"고용률(15\~64세)") == "고용률(15~64세)"


def test_latex_survives_untouched() -> None:
    assert unescape(r"자본 \(\cdot\) 금융계정") == r"자본 \(\cdot\) 금융계정"
    assert unescape(r"자동차( $\triangle$ 12.4%)") == r"자동차( $\triangle$ 12.4%)"


def test_a_text_block_is_stored_unescaped() -> None:
    tally = _Tally()
    row = _row_for(None, None, {"type": "text", "text": r"\- 수도권 0.1% 상승"}, tally)

    assert row is not None
    assert row.content == f"{BULLET} 수도권 0.1% 상승"


def test_a_list_block_is_stored_unescaped() -> None:
    tally = _Tally()
    row = _row_for(
        None,
        None,
        {"type": "list", "list_items": [r"\* 제조업 PMI: 58.1", r"\- 수출물량 증가"]},
        tally,
    )

    assert row is not None
    assert row.content == f"* 제조업 PMI: 58.1 \n {BULLET} 수출물량 증가"


def test_a_table_caption_is_stored_unescaped_and_the_body_is_not_touched() -> None:
    tally = _Tally()
    body = r"<table><tr><td> $\triangle 3.5$ </td></tr></table>"
    row = _row_for(
        None,
        None,
        {
            "type": "table",
            "table_caption": [r"\* 고용률(15\~64세)"],
            "table_body": body,
        },
        tally,
    )

    assert row is not None
    assert row.caption == "* 고용률(15~64세)"
    assert row.content == body
