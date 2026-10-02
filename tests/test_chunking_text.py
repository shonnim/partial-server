"""The LLM form of a chunk, derived from the embedder's form.

``llm_text`` is used twice — on every fresh chunk at save time, and by the
backfill button for chunks stored before the column existed or filled by an
earlier rule — so it must hold for every shape emb_text takes: the bracketed
breadcrumb on its own line, the empty breadcrumb ``[]``, and rows from
before breadcrumbs at all.
"""

from app.services.chunking import llm_text


def test_breadcrumb_line_is_stripped_off() -> None:
    emb = "[I. 채무의 현명한 관리 > 1. 세계 채무 현황]\n□ 세계 부채가 늘었다\n○ 선진국이 이끌었다"
    path = "[I. 채무의 현명한 관리 > 1. 세계 채무 현황]"

    assert llm_text(emb, path) == "□ 세계 부채가 늘었다\n○ 선진국이 이끌었다"


def test_empty_breadcrumb_is_stripped_too() -> None:
    # Content before the first I./1. heading is chunked under "[]".
    assert llm_text("[]\n□ 서문의 문단", "[]") == "□ 서문의 문단"


def test_no_section_path_leaves_the_text_as_it_is() -> None:
    assert llm_text("본문 그대로", None) == "본문 그대로"


def test_emb_text_not_opening_with_the_path_is_kept_whole() -> None:
    # A row that was edited by hand, or a path that no longer matches: the
    # body must not be cut on a guess, so the whole emb_text is the body.
    emb = "□ 브레드크럼 없는 본문"
    assert llm_text(emb, "[I. 머리말]") == emb


def test_a_chunk_that_is_only_its_breadcrumb_becomes_empty() -> None:
    assert llm_text("[I. 머리말]", "[I. 머리말]") == ""


def test_multiline_body_survives_unchanged() -> None:
    emb = "[I. A > 1. B]\n<표 I-1> 전망\n| 연도 | 값 |\n| --- | --- |\n| 2024 | 1.2 |"
    assert llm_text(emb, "[I. A > 1. B]") == (
        "<표 I-1> 전망\n| 연도 | 값 |\n| --- | --- |\n| 2024 | 1.2 |"
    )
