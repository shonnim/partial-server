"""How an extracted title is normalised, and how a group picks its strategy.

The corpus numbers its headings "I. 서론" / "2. 개요"; everything a parser or
a designer put in the way of that pattern — the single-character roman
numerals Ⅱ and Ⅲ, dot leaders left on the end, a dash where the dot belongs —
is repaired before the title is stored.
"""

from app.services.header_extraction import normalize_title
from app.services.header_pages import (
    DEFAULT_STRATEGY,
    GROUP_STRATEGIES,
    GroupStrategy,
    strategy_for,
)

# ---- the roman-numeral characters --------------------------------------------


def test_roman_characters_become_letters() -> None:
    assert normalize_title("Ⅱ. 재정 동향") == "II. 재정 동향"
    assert normalize_title("Ⅲ. 결론") == "III. 결론"


def test_roman_characters_are_replaced_anywhere_in_the_title() -> None:
    assert normalize_title("부록 Ⅱ") == "부록 II"


def test_other_roman_numerals_arrive_as_letters_already() -> None:
    assert normalize_title("IV. 전망") == "IV. 전망"


# ---- trailing dot leaders -----------------------------------------------------


def test_trailing_dots_come_off() -> None:
    assert normalize_title("1. 개요....") == "1. 개요"


def test_trailing_ellipsis_comes_off() -> None:
    assert normalize_title("1. 개요 …") == "1. 개요"


def test_a_single_full_stop_is_not_a_leader() -> None:
    assert normalize_title("각주는 생략함.") == "각주는 생략함."


def test_mixed_dots_and_spaces_come_off_whole() -> None:
    assert normalize_title("2. 전망 .. . ..") == "2. 전망"


# ---- the leading ordinal ------------------------------------------------------


def test_dashes_after_the_ordinal_become_the_dot() -> None:
    assert normalize_title("I -- 서론") == "I. 서론"
    assert normalize_title("II – 동향") == "II. 동향"


def test_a_bare_space_after_the_ordinal_gets_the_dot() -> None:
    assert normalize_title("II 재정 전망") == "II. 재정 전망"
    assert normalize_title("3 예산 집행") == "3. 예산 집행"


def test_already_normal_titles_pass_unchanged() -> None:
    assert normalize_title("I. 서론") == "I. 서론"
    assert normalize_title("2. 개요") == "2. 개요"


def test_hierarchical_numbering_is_not_a_separator() -> None:
    # The dot of "1.2" binds the ordinal together; forcing "1. 2 예산" would
    # break the numbering the contents page actually uses.
    assert normalize_title("1.2 예산 동향") == "1.2 예산 동향"


def test_a_lone_roman_letter_before_an_english_word_is_a_sentence() -> None:
    assert normalize_title("I think therefore") == "I think therefore"


def test_titles_without_an_ordinal_are_untouched() -> None:
    assert normalize_title("제1장 총론") == "제1장 총론"
    assert normalize_title("Internet governance") == "Internet governance"


def test_a_year_is_not_an_ordinal() -> None:
    # Ordinals are at most two digits; a title opening with a year keeps it.
    assert normalize_title("2023 예산안 분석") == "2023 예산안 분석"


def test_the_repairs_compose() -> None:
    assert normalize_title("Ⅱ -- 재정 동향....") == "II. 재정 동향"


# ---- picking a group's strategy -----------------------------------------------


def test_no_group_means_the_default_strategy() -> None:
    assert strategy_for(None) is DEFAULT_STRATEGY


def test_an_unregistered_group_means_the_default_strategy() -> None:
    assert strategy_for("never-heard-of-it") is DEFAULT_STRATEGY


def test_a_registered_group_gets_its_own_strategy(monkeypatch) -> None:
    strategy = GroupStrategy(start_page=2, end_page=4, llm="solar")
    monkeypatch.setitem(GROUP_STRATEGIES, "imf-1", strategy)

    assert strategy_for("imf-1") is strategy
