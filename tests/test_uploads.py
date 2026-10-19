"""Path, filename, and metadata handling behind the parsing tree's upload pop-up."""

from io import BytesIO

import pytest
from pypdf import PdfWriter

from app.services.uploads import (
    InvalidUploadFile,
    InvalidUploadPath,
    build_key,
    category_of,
    clean_pdf_filename,
    clean_subpath,
    count_pages,
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


# ---- category from the key -----------------------------------------------------


def test_category_is_the_keys_first_folder() -> None:
    key = "재정간행물/월간나라재정/2026_월간나라재정_6월/한국재정정보원_2026 월간나라재정_6월.pdf"
    assert category_of(key) == "재정간행물"
    assert category_of("reports/doc.pdf") == "reports"


def test_category_of_a_root_key_is_none() -> None:
    assert category_of("doc.pdf") is None


# ---- page count ----------------------------------------------------------------


def _pdf_with_pages(n: int) -> bytes:
    writer = PdfWriter()
    for _ in range(n):
        writer.add_blank_page(width=595, height=842)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def test_count_pages_reads_the_pdf() -> None:
    assert count_pages(_pdf_with_pages(1)) == 1
    assert count_pages(_pdf_with_pages(3)) == 3


def test_count_pages_is_none_for_unreadable_bytes() -> None:
    assert count_pages(b"%PDF-1.7 but nothing else") is None
