"""The contents-marker search behind the has_header verdict."""

from app.services.headers import (
    contents_page_index,
    find_marker,
    find_split_marker,
    find_split_marker_text,
)


def test_find_marker_knows_both_spellings_and_latin_case() -> None:
    assert find_marker("목 차") == "목 차"
    assert find_marker("<목차>") == "목차"
    assert find_marker("차 례") == "차 례"
    assert find_marker("CONTENTS") == "contents"
    assert find_marker("본문") is None


def _block(text: str, page: int, level: int | None = 1) -> dict:
    block: dict = {"type": "text", "text": text, "page_idx": page}
    if level is not None:
        block["text_level"] = level
    return block


def test_split_marker_survives_an_interleaved_running_header() -> None:
    # A large letter-spaced "목 차" parses as one block per syllable, with the
    # running header read between them — the case that used to decide NO.
    blocks = [
        _block("목", 2),
        _block("재정동향", 2),
        _block("차", 2),
    ]
    assert find_split_marker(blocks) == ("목차", 2)
    assert contents_page_index(blocks) == 2


def test_split_marker_skips_a_non_marker_character_between_syllables() -> None:
    blocks = [_block("목", 2), _block("3", 2), _block("차", 2)]
    assert find_split_marker(blocks) == ("목차", 2)


def test_split_marker_reads_through_an_ocr_confusable_syllable() -> None:
    # The same split title, but the OCR read the large "차" as the look-alike
    # hanzi "补" — seen in the wild on page 2 of a 재정동향 volume.
    blocks = [
        _block("목", 2),
        _block("재정동향", 2),
        _block("补", 2),
    ]
    assert find_split_marker(blocks) == ("목차", 2)
    assert contents_page_index(blocks) == 2
    # The confusable is also mapped in a whole-string search and in markdown.
    assert find_marker("목 补") == "목 차"
    assert find_split_marker_text("# 목\n\n재정동향\n\n# 补\n") == "목차"


def test_split_marker_spells_차례_too() -> None:
    blocks = [_block("차", 1), _block("례", 1)]
    assert find_split_marker(blocks) == ("차례", 1)


def test_split_marker_needs_the_syllables_on_one_page_in_order() -> None:
    assert find_split_marker([_block("목", 1), _block("차", 2)]) is None
    assert find_split_marker([_block("차", 3), _block("목", 3)]) is None
    assert find_split_marker([_block("목", 4)]) is None


def test_split_marker_tolerates_a_missing_page_index() -> None:
    blocks = [
        {"type": "text", "text": "목", "text_level": 1},
        {"type": "text", "text": "차", "text_level": 1},
    ]
    assert find_split_marker(blocks) == ("목차", None)


def test_split_marker_handles_junk_shapes() -> None:
    assert find_split_marker(None) is None
    assert find_split_marker("목차") is None
    assert find_split_marker([None, 7, {"type": "text"}, {"text": 3}]) is None


def test_split_marker_text_reads_the_split_title_out_of_markdown() -> None:
    # The pages-parse markdown twin of the split title: one heading line per
    # syllable, the running header between them.
    assert find_split_marker_text("# 목\n\n재정동향\n\n# 차\n\n본문…") == "목차"
    assert find_split_marker_text("- 차\n- 례") == "차례"
    assert find_split_marker_text("# 목차 아님, 본문") is None
    assert find_split_marker_text("") is None


def test_contents_page_index_prefers_a_whole_block_marker() -> None:
    blocks = [_block("목", 5), _block("차", 5), _block("목 차", 1)]
    assert contents_page_index(blocks) == 1
