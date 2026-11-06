"""What the Grouping page's group id input offers as suggestions."""

from app.services import grouping


def test_registered_strategy_ids_are_offered_before_use(monkeypatch) -> None:
    # No document carries bpa-1 yet; the registries must still offer it.
    monkeypatch.setattr(grouping, "group_ids_in_use", lambda db: ["budget-2024"])

    offered = grouping.suggested_group_ids(db=None)

    assert "bpa-1" in offered  # header extraction's GROUP_STRATEGIES
    assert "imf-1" in offered and "imf-2" in offered  # chunking's CHUNKERS
    assert "budget-2024" in offered  # labels already in use stay
    assert offered == sorted(offered)


def test_an_id_both_in_use_and_registered_is_offered_once(monkeypatch) -> None:
    monkeypatch.setattr(grouping, "group_ids_in_use", lambda db: ["bpa-1", "imf-1"])

    offered = grouping.suggested_group_ids(db=None)

    assert offered.count("bpa-1") == 1
    assert offered.count("imf-1") == 1
