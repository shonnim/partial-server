"""The Grouping page's group list: known ids, descriptions, counts."""

from app.models.group import Group
from app.services import grouping


def test_registered_strategy_ids_are_listed_before_use(monkeypatch) -> None:
    # No document carries bpa-1 yet; the registries must still list it.
    monkeypatch.setattr(grouping, "_document_counts", lambda db: {"budget-2024": 7})
    monkeypatch.setattr(grouping, "_descriptions", lambda db: {})

    groups = grouping.list_groups(db=None)
    by_id = {g.group_id: g for g in groups}

    assert "bpa-1" in by_id  # header extraction's GROUP_STRATEGIES
    assert "imf-1" in by_id and "oft-1" in by_id  # chunking's CHUNKERS
    assert by_id["budget-2024"].documents == 7  # labels in use stay, counted
    assert by_id["bpa-1"].documents == 0
    assert [g.group_id for g in groups] == sorted(g.group_id for g in groups)


def test_descriptions_and_counts_merge_onto_one_row_per_id(monkeypatch) -> None:
    monkeypatch.setattr(grouping, "_document_counts", lambda db: {"oft-1": 12})
    monkeypatch.setattr(
        grouping,
        "_descriptions",
        lambda db: {"oft-1": "해외재정동향", "old-series": "구 시리즈"},
    )

    groups = grouping.list_groups(db=None)
    by_id = {g.group_id: g for g in groups}

    assert [g.group_id for g in groups].count("oft-1") == 1
    assert by_id["oft-1"].description == "해외재정동향"
    assert by_id["oft-1"].documents == 12
    # A described id with no documents and no strategy is still listed.
    assert by_id["old-series"].description == "구 시리즈"
    assert by_id["old-series"].documents == 0
    # A known id never described has no description, not an empty string.
    assert by_id["bpa-1"].description is None


# ---- saving a description ------------------------------------------------------


class StubDb:
    """Enough of a Session for ``save_description``: one lookup, one write."""

    def __init__(self, row: Group | None = None) -> None:
        self.row = row
        self.added: list[Group] = []
        self.deletes = 0
        self.committed = False

    def scalar(self, stmt):
        return self.row

    def add(self, obj) -> None:
        self.added.append(obj)

    def execute(self, stmt):
        self.deletes += 1

    def commit(self) -> None:
        self.committed = True


def test_a_new_description_inserts_a_row() -> None:
    db = StubDb()
    grouping.save_description(db, "oft-1", "해외재정동향")

    assert len(db.added) == 1
    assert (db.added[0].group_id, db.added[0].description) == ("oft-1", "해외재정동향")
    assert db.committed


def test_an_existing_description_is_updated_in_place() -> None:
    row = Group(group_id="oft-1", description="옛 이름")
    db = StubDb(row)
    grouping.save_description(db, "oft-1", "해외재정동향")

    assert row.description == "해외재정동향"
    assert db.added == [] and db.deletes == 0
    assert db.committed


def test_an_empty_description_removes_the_row() -> None:
    db = StubDb(Group(group_id="oft-1", description="해외재정동향"))
    grouping.save_description(db, "oft-1", None)

    assert db.deletes == 1 and db.added == []
    assert db.committed


def test_clearing_a_description_that_was_never_stored_writes_nothing() -> None:
    db = StubDb()
    grouping.save_description(db, "oft-1", None)

    assert db.deletes == 0 and db.added == []
