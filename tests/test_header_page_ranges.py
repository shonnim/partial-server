"""How the Pages method reads start/end page indexes out of a content list.

The cases that used to come back ``(None, None)`` although the parsed JSON
knew the page: a printed page number the parser never captured, and a heading
whose text cannot be matched but whose printed number can.
"""

from app.services.header_extraction import ExtractedHeader, page_index_ranges


def _text(page: int, text: str, heading: bool = False) -> dict:
    block = {"type": "text", "page_idx": page, "text": text}
    if heading:
        block["text_level"] = 1
    return block


def _page_number(page: int, printed: int) -> dict:
    return {"type": "page_number", "page_idx": page, "text": f"- {printed} -"}


# A contents page on page 1, two chapters, body text between and after them.
CONTENTS = _text(1, "목 차")


def test_located_heading_confirmed_by_printed_number() -> None:
    blocks = [
        CONTENTS,
        _page_number(3, 5),
        _text(3, "I. 서론", heading=True),
        _text(3, "본문"),
        _page_number(6, 11),
        _text(6, "II. 결론", heading=True),
        _text(6, "본문"),
    ]
    headers = [
        ExtractedHeader(title="I. 서론", level=1, start_page=5),
        ExtractedHeader(title="II. 결론", level=1, start_page=11),
    ]

    assert page_index_ranges(headers, blocks) == [(3, 5), (6, 6)]


def test_missing_page_number_blocks_do_not_discard_a_located_heading() -> None:
    # No page_number blocks at all: the printed numbers have nothing to be
    # checked against, and the located headings must stand on their own.
    blocks = [
        CONTENTS,
        _text(3, "I. 서론", heading=True),
        _text(3, "본문"),
        _text(6, "II. 결론", heading=True),
    ]
    headers = [
        ExtractedHeader(title="I. 서론", level=1, start_page=5),
        ExtractedHeader(title="II. 결론", level=1, start_page=11),
    ]

    assert page_index_ranges(headers, blocks) == [(3, 5), (6, 6)]


def test_disagreeing_printed_number_still_leaves_the_start_unknown() -> None:
    # The number 5 is printed on page 4, but the matched heading sits on
    # page 3: genuine ambiguity, recorded as unknown rather than guessed.
    blocks = [
        CONTENTS,
        _text(3, "I. 서론", heading=True),
        _page_number(4, 5),
        _text(4, "본문"),
    ]
    headers = [ExtractedHeader(title="I. 서론", level=1, start_page=5)]

    assert page_index_ranges(headers, blocks) == [(None, 4)]


def test_unlocatable_heading_falls_back_to_its_printed_number() -> None:
    # OCR mangled the second heading beyond matching, but the content list
    # knows which page printed number 11 is on.
    blocks = [
        CONTENTS,
        _page_number(3, 5),
        _text(3, "I. 서론", heading=True),
        _text(3, "본문"),
        _page_number(6, 11),
        _text(6, "Ⅱ 걸론~", heading=True),  # typo: never matches "II. 결론"
        _text(7, "본문"),
    ]
    headers = [
        ExtractedHeader(title="I. 서론", level=1, start_page=5),
        ExtractedHeader(title="II. 결론", level=1, start_page=11),
    ]

    # The first chapter cannot be proven to end on page 5 — whether the
    # unlocated heading opens page 6 is unknowable — so it runs onto it.
    assert page_index_ranges(headers, blocks) == [(3, 6), (6, 7)]


def test_printed_fallback_never_points_before_the_contents_page() -> None:
    # Front matter restarted the numbering: "3" is printed on page 0 before
    # the contents page, and again nowhere else. The fallback must not drag
    # the chapter in front of its own contents list.
    blocks = [
        _page_number(0, 3),
        _text(0, "머리말"),
        CONTENTS,
        _text(4, "본문"),
    ]
    headers = [ExtractedHeader(title="I. 서론", level=1, start_page=3)]

    assert page_index_ranges(headers, blocks) == [(None, 4)]
