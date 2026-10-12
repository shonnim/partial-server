"""Path and filename handling behind the parsing tree's upload pop-up."""

import pytest

from app.services.uploads import (
    InvalidUploadFile,
    InvalidUploadPath,
    build_key,
    clean_pdf_filename,
    clean_subpath,
    validate_folder_path,
)

# ---- the folder the "+" button sits on ---------------------------------------


def test_folder_path_passes_through_verbatim() -> None:
    assert validate_folder_path("경제보고서/2024 [최종]") == "경제보고서/2024 [최종]"


def test_folder_path_root_is_empty() -> None:
    assert validate_folder_path("") == ""
    assert validate_folder_path("  /  ") == ""


def test_folder_path_sheds_surrounding_slashes() -> None:
    assert validate_folder_path("/reports/2024/") == "reports/2024"


@pytest.mark.parametrize("raw", ["a/../b", "..", "a//b", "a\\b", "a/./b"])
def test_folder_path_rejects_traversal_and_blank_segments(raw: str) -> None:
    with pytest.raises(InvalidUploadPath):
        validate_folder_path(raw)


# ---- the user-typed subfolder ------------------------------------------------


def test_subpath_empty_means_the_folder_itself() -> None:
    assert clean_subpath("") == ""
    assert clean_subpath("   ") == ""


def test_subpath_normalises_separators_and_blanks() -> None:
    assert clean_subpath(" 2024 / reports ") == "2024/reports"
    assert clean_subpath("2024\\reports") == "2024/reports"
    assert clean_subpath("/2024//reports/") == "2024/reports"


@pytest.mark.parametrize("raw", ["..", "a/../b", "./a"])
def test_subpath_rejects_dot_segments(raw: str) -> None:
    with pytest.raises(InvalidUploadPath):
        clean_subpath(raw)


# ---- the uploaded filename ---------------------------------------------------


def test_filename_keeps_a_plain_pdf_name() -> None:
    assert clean_pdf_filename("보고서 (최종).PDF") == "보고서 (최종).PDF"


def test_filename_drops_any_directory_part() -> None:
    assert clean_pdf_filename("C:\\Users\\me\\doc.pdf") == "doc.pdf"
    assert clean_pdf_filename("some/dir/doc.pdf") == "doc.pdf"


@pytest.mark.parametrize("raw", [None, "", "doc.txt", ".pdf", "dir/.pdf"])
def test_filename_rejects_non_pdfs(raw: str | None) -> None:
    with pytest.raises(InvalidUploadFile):
        clean_pdf_filename(raw)


# ---- the object key ----------------------------------------------------------


def test_key_joins_folder_subpath_and_name() -> None:
    assert build_key("reports", "2024", "doc.pdf") == "reports/2024/doc.pdf"


def test_key_skips_the_empty_parts() -> None:
    assert build_key("reports", "", "doc.pdf") == "reports/doc.pdf"
    assert build_key("", "", "doc.pdf") == "doc.pdf"
