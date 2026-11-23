"""Which documents a Parse docs click takes, with and without Force parsing."""

from app.mineru import BACKEND
from app.services.parse_runs import needs_parse, skip_reason

# ---- the ordinary run ----------------------------------------------------------


def test_never_parsed_goes_in() -> None:
    assert needs_parse(None)
    assert skip_reason(None, force=False) is None


def test_other_engine_goes_in() -> None:
    assert needs_parse("hybrid-engine")
    assert skip_reason("hybrid-engine", force=False) is None


def test_already_vlm_is_skipped_with_the_reason() -> None:
    assert not needs_parse(BACKEND)
    assert skip_reason(BACKEND, force=False) == f"already parsed with {BACKEND}"


# ---- Force parsing -------------------------------------------------------------


def test_force_takes_the_already_parsed_document() -> None:
    assert skip_reason(BACKEND, force=True) is None


def test_force_changes_nothing_for_the_rest() -> None:
    assert skip_reason(None, force=True) is None
    assert skip_reason("hybrid-engine", force=True) is None
