"""The name normalisation and matching rules behind the Mapping check.

The DB-facing query is exercised separately; these tests pin the pure rules —
above all that the source system's "2009년도 성과계획서(산림청)" and the
uploaded "2009년도_성과계획서(산림청).pdf" count as the same name.
"""

import uuid

from app.schemas.mapping import MappingEntry
from app.services.mapping import FolderDoc, normalize_name


def entry(name: str, seq: int = 1) -> MappingEntry:
    return MappingEntry(
        document_type="T",
        name=name,
        source_document_id="1",
        atch_file_id="F01",
        atch_file_seq=seq,
    )


def test_underscores_and_extension_are_ignored() -> None:
    assert normalize_name("2009년도_성과계획서(산림청).pdf") == normalize_name(
        "2009년도 성과계획서(산림청)"
    )


def test_whitespace_is_collapsed_and_case_folded() -> None:
    assert normalize_name("  IMF  World   Outlook ") == normalize_name(
        "imf world outlook"
    )


def test_unicode_forms_agree() -> None:
    # macOS stores Hangul decomposed (NFD); the source system sends it composed.
    import unicodedata

    decomposed = unicodedata.normalize("NFD", "성과계획서")
    assert decomposed != "성과계획서"
    assert normalize_name(decomposed) == normalize_name("성과계획서")


def test_document_extensions_are_dropped() -> None:
    # The source 파일명 carries whatever extension the publication used.
    assert normalize_name("2026년 나라살림 예산개요.pdf") == normalize_name(
        "2026년_나라살림_예산개요"
    )
    assert normalize_name("보도자료.hwp") == normalize_name("보도자료")
    assert normalize_name("notes.PDF") == "notes"
    # Only one trailing extension goes, and only a known one — a name that
    # merely ends in a dot-word stays intact.
    assert normalize_name("report.pdf.pdf") == "report.pdf"
    assert normalize_name("개정안 제2.5차") == "개정안 제2.5차"


def test_entry_coerces_numeric_post_id() -> None:
    e = MappingEntry.model_validate(
        {
            "유형명": "IMF",
            "파일명": "x.pdf",
            "게시물ID": 51720,
            "첨부파일ID": "F",
            "첨부순번": 1,
        }
    )
    assert e.source_document_id == "51720"
    assert e.name == "x.pdf"
    assert e.atch_file_seq == 1


def test_check_matches_and_reports_leftovers(monkeypatch) -> None:
    from app.services import mapping

    docs = [
        FolderDoc(id=uuid.uuid4(), filename="2009년도_성과계획서(산림청).pdf"),
        FolderDoc(id=uuid.uuid4(), filename="남는_파일.pdf"),
    ]
    monkeypatch.setattr(mapping, "folder_documents", lambda db, path: docs)

    result, error = mapping.check_mapping(
        None, "재정간행물/IMF", [entry("2009년도 성과계획서(산림청)"), entry("없는 문서")]
    )

    assert error is None
    assert result.matched == 1
    assert result.not_found == 1
    assert result.checks[0].doc is docs[0]
    assert result.unmapped == ["남는_파일.pdf"]


def test_entry_reads_the_camelcase_format() -> None:
    # 재정보고서 및 문서 lists with camelCase keys; odtId plays the 게시물ID
    # role, and the extension arrives separately in fileExt.
    e = MappingEntry.model_validate(
        {
            "dpFileNm": "2024회계연도 교육비특별회계 세입세출 본예산 분석",
            "odtId": "2FC01MV65ON1470E66725U77K",
            "atchFileId": "C9210000000000000001",
            "atchFileSeq": 8,
            "acntYr": "2024",
            "fileExt": "pdf",
            "allDtaClsNm": "지방교육재정",
            "downloadUrl": "https://www.openfiscaldata.go.kr/files/C9210000000000000001/8",
        }
    )
    assert e.name == "2024회계연도 교육비특별회계 세입세출 본예산 분석.pdf"
    assert e.source_document_id == "2FC01MV65ON1470E66725U77K"
    assert e.atch_file_id == "C9210000000000000001"
    assert e.atch_file_seq == 8
    assert e.document_type == "지방교육재정"


def test_camelcase_zip_entry_keeps_its_archive_extension() -> None:
    from app.services.mapping import is_archive_name

    e = MappingEntry.model_validate(
        {
            "dpFileNm": "2024 지방교육재정 자료집",
            "odtId": "X",
            "atchFileId": "C1",
            "atchFileSeq": 1,
            "fileExt": "zip",
            "allDtaClsNm": "지방교육재정",
        }
    )
    assert e.name == "2024 지방교육재정 자료집.zip"
    assert is_archive_name(e.name)


def test_zip_entry_matches_folder_named_like_the_archive(monkeypatch) -> None:
    # A zip's PDFs were registered as separate documents inside a folder named
    # after the archive; the entry stands for all of them, subfolders included.
    from app.services import mapping

    docs = [
        FolderDoc(id=uuid.uuid4(), filename="본문.pdf", folder="p/2026년_자료집"),
        FolderDoc(id=uuid.uuid4(), filename="부록.pdf", folder="p/2026년_자료집/부록"),
        FolderDoc(id=uuid.uuid4(), filename="다른_파일.pdf", folder="p"),
    ]
    monkeypatch.setattr(mapping, "folder_documents", lambda db, path: docs)

    result, error = mapping.check_mapping(None, "p", [entry("2026년 자료집.zip")])

    assert error is None
    assert result.matched == 1
    check = result.checks[0]
    assert check.ok
    assert check.folder == "p/2026년_자료집"
    assert {d.filename for d in check.resolved} == {"본문.pdf", "부록.pdf"}
    assert result.unmapped == ["다른_파일.pdf"]


def test_non_archive_entry_never_matches_a_folder(monkeypatch) -> None:
    from app.services import mapping

    docs = [FolderDoc(id=uuid.uuid4(), filename="본문.pdf", folder="p/자료집")]
    monkeypatch.setattr(mapping, "folder_documents", lambda db, path: docs)

    result, error = mapping.check_mapping(None, "p", [entry("자료집.pdf")])

    assert error is None
    assert result.matched == 0
    assert result.not_found == 1


def test_zip_entry_with_two_matching_folders_is_ambiguous(monkeypatch) -> None:
    from app.services import mapping

    docs = [
        FolderDoc(id=uuid.uuid4(), filename="a.pdf", folder="p/2018/자료집"),
        FolderDoc(id=uuid.uuid4(), filename="b.pdf", folder="p/2019/자료집"),
    ]
    monkeypatch.setattr(mapping, "folder_documents", lambda db, path: docs)

    result, error = mapping.check_mapping(None, "p", [entry("자료집.zip")])

    assert error is None
    assert result.matched == 0
    assert result.not_found == 0
    assert result.ambiguous == 1
    assert result.checks[0].folder is None


def test_save_writes_the_zip_ids_onto_every_file_in_the_folder(monkeypatch) -> None:
    from app.services import mapping

    docs = [
        FolderDoc(id=uuid.uuid4(), filename="본문.pdf", folder="p/자료집"),
        FolderDoc(id=uuid.uuid4(), filename="부록.pdf", folder="p/자료집"),
    ]
    monkeypatch.setattr(mapping, "folder_documents", lambda db, path: docs)

    class FakeDb:
        def __init__(self) -> None:
            self.rows: list[dict] = []
            self.committed = False

        def execute(self, stmt, params):
            self.rows = params

        def commit(self) -> None:
            self.committed = True

    db = FakeDb()
    result, error = mapping.save_mapping(db, "p", [entry("자료집.zip", seq=2)])

    assert error is None
    assert result.saved == 2
    assert db.committed
    assert {row["id"] for row in db.rows} == {d.id for d in docs}
    assert all(
        row["source_document_id"] == "1"
        and row["atch_file_id"] == "F01"
        and row["atch_file_seq"] == 2
        for row in db.rows
    )


def test_duplicate_entries_are_flagged(monkeypatch) -> None:
    from app.services import mapping

    docs = [FolderDoc(id=uuid.uuid4(), filename="보고서.pdf")]
    monkeypatch.setattr(mapping, "folder_documents", lambda db, path: docs)

    result, error = mapping.check_mapping(
        None, "p", [entry("보고서"), entry("보고서")]
    )

    assert error is None
    assert result.matched == 0
    assert result.ambiguous == 2


def test_save_writes_only_clean_matches(monkeypatch) -> None:
    from app.services import mapping

    docs = [
        FolderDoc(id=uuid.uuid4(), filename="2026년_나라살림_예산개요.pdf"),
        FolderDoc(id=uuid.uuid4(), filename="남는_파일.pdf"),
    ]
    monkeypatch.setattr(mapping, "folder_documents", lambda db, path: docs)

    class FakeDb:
        def __init__(self) -> None:
            self.rows: list[dict] = []
            self.committed = False

        def execute(self, stmt, params):
            self.rows = params

        def commit(self) -> None:
            self.committed = True

    db = FakeDb()
    good = entry("2026년 나라살림 예산개요.pdf", seq=3)
    result, error = mapping.save_mapping(db, "p", [good, entry("없는 문서")])

    assert error is None
    assert result.saved == 1
    assert db.committed
    assert db.rows == [
        {
            "id": docs[0].id,
            "source_document_id": "1",
            "atch_file_id": "F01",
            "atch_file_seq": 3,
        }
    ]


def test_save_with_nothing_matched_touches_nothing(monkeypatch) -> None:
    from app.services import mapping

    docs = [FolderDoc(id=uuid.uuid4(), filename="보고서.pdf")]
    monkeypatch.setattr(mapping, "folder_documents", lambda db, path: docs)

    class ExplodingDb:
        def execute(self, stmt, params):  # pragma: no cover - must not run
            raise AssertionError("no write expected")

        commit = execute

    result, error = mapping.save_mapping(ExplodingDb(), "p", [entry("없는 문서")])

    assert error is None
    assert result.saved == 0
