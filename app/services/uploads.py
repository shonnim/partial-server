"""Uploading PDFs into the corpus from the parsing tree.

Each folder of the tree carries a "+" button whose pop-up posts here: the
folder's own path, an optional subfolder typed by the user, and one or more
PDF files. The bytes go to RustFS under a key that mirrors the folder path,
and each file becomes a Document row — or refreshes the existing row when the
key is already registered, so re-uploading the same file is not a duplicate.
"""

from dataclasses import dataclass
from io import BytesIO

from fastapi import UploadFile
from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.document import Document
from app.storage import put_object

# Files accepted per upload. The pop-up targets one folder at a time; anything
# bigger than this is a bulk import, which does not belong in a request.
MAX_UPLOAD_FILES = 100

# One PDF is read fully into memory before it goes to RustFS, so it gets a
# ceiling. The corpus documents are a few MB each; this is far above them.
MAX_UPLOAD_BYTES = 500 * 1024 * 1024

PDF_CONTENT_TYPE = "application/pdf"
_PDF_MAGIC = b"%PDF-"


class InvalidUploadPath(ValueError):
    """A folder path or subfolder that cannot become part of an object key."""


class InvalidUploadFile(ValueError):
    """One file of the batch that cannot be stored; the rest still can."""


@dataclass(frozen=True)
class UploadOutcome:
    """What happened to one file of the batch."""

    filename: str
    key: str | None = None
    size_bytes: int = 0
    # Counted from the uploaded bytes; None when the PDF could not be read.
    pages: int | None = None
    # True when a Document with this key already existed: its object was
    # replaced and its row refreshed rather than a second row inserted.
    replaced: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def validate_folder_path(raw: str) -> str:
    """The tree folder the "+" button sits on, checked but kept verbatim.

    Folder paths come out of existing object keys, so beyond trimming slashes
    nothing is rewritten — a changed path would no longer name the folder the
    button was on. '' is the corpus root and is valid.
    """
    path = raw.strip().strip("/")
    if not path:
        return ""
    if "\\" in path or any(s.strip() in {"", ".", ".."} for s in path.split("/")):
        raise InvalidUploadPath("The folder path could not be used.")
    return path


def clean_subpath(raw: str) -> str:
    """Normalise the user-typed subfolder into key segments.

    Backslashes read as separators, blank segments collapse, and dot segments
    are refused — an object key is not a filesystem path to walk out of.
    '' means "into the folder itself".
    """
    parts = [p.strip() for p in raw.strip().replace("\\", "/").split("/")]
    parts = [p for p in parts if p]
    if any(p in {".", ".."} for p in parts):
        raise InvalidUploadPath("The subfolder cannot contain '.' or '..' segments.")
    return "/".join(parts)


def clean_pdf_filename(raw: str | None) -> str:
    """The bare filename an upload will be stored under.

    Any directory part the browser sent is dropped, and only ``.pdf`` names
    are accepted — the pop-up's file input already filters, but the server
    cannot rely on that.
    """
    name = (raw or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name:
        raise InvalidUploadFile("The file has no name.")
    if not name.lower().endswith(".pdf") or len(name) == len(".pdf"):
        raise InvalidUploadFile("Only .pdf files can be uploaded.")
    return name


def build_key(folder_path: str, subpath: str, filename: str) -> str:
    """The object key for one upload: folder, then subfolder, then the name."""
    return "/".join(part for part in (folder_path, subpath, filename) if part)


def category_of(key: str) -> str | None:
    """The document's category: the first folder of its object key.

    The corpus is laid out as ``{category}/.../file.pdf``, so the top-level
    folder is the category. A key with no folder — a file at the bucket
    root — has none.
    """
    first, sep, _ = key.partition("/")
    return first if sep else None


def count_pages(data: bytes) -> int | None:
    """How many pages the PDF holds, or None when it cannot be read.

    The count is metadata on the row, not an extraction result: a PDF whose
    structure pypdf cannot walk (encrypted, damaged cross-reference table)
    still uploads, with the count left unknown the way a never-parsed
    document's is.
    """
    try:
        return len(PdfReader(BytesIO(data)).pages)
    except Exception:
        return None


def _read_pdf(upload: UploadFile) -> bytes:
    """Read one upload fully, refusing what is oversized or not a PDF."""
    data = upload.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise InvalidUploadFile(
            f"Larger than the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit."
        )
    if not data.startswith(_PDF_MAGIC):
        raise InvalidUploadFile("Not a PDF — the file does not start with %PDF.")
    return data


def _register(
    db: Session,
    *,
    bucket: str,
    key: str,
    filename: str,
    size: int,
    pages: int | None,
) -> bool:
    """Insert the Document row for ``key``, or refresh the one that holds it.

    Returns True when the key was already registered. Matching on the key is
    what makes re-uploading idempotent: the same file into the same folder is
    one Document, not a growing pile of rows over one object. The category is
    the key's top-level folder either way.
    """
    category = category_of(key)
    existing = db.scalars(
        select(Document).where(Document.bucket == bucket, Document.s3_key == key)
    ).first()
    if existing is not None:
        existing.filename = filename
        existing.size_bytes = size
        existing.content_type = PDF_CONTENT_TYPE
        existing.category = category
        # The replacement file may have a different length; only a readable
        # count overwrites, so a bad read cannot blank a known one.
        if pages is not None:
            existing.pages = pages
        return True

    db.add(
        Document(
            bucket=bucket,
            s3_key=key,
            filename=filename,
            size_bytes=size,
            content_type=PDF_CONTENT_TYPE,
            category=category,
            pages=pages,
        )
    )
    return False


def upload_pdfs(
    db: Session, *, folder_path: str, subpath: str, files: list[UploadFile]
) -> list[UploadOutcome]:
    """Store a batch of PDFs under one folder and register each as a Document.

    A bad path refuses the whole batch (InvalidUploadPath); a bad file refuses
    only itself and comes back as a failed outcome, so one stray .txt does not
    sink the PDFs next to it. Each file commits on its own — the row and the
    object land together, and an error later in the batch cannot take back
    what already succeeded.
    """
    folder = validate_folder_path(folder_path)
    sub = clean_subpath(subpath)
    bucket = get_settings().bucket_documents

    outcomes: list[UploadOutcome] = []
    for upload in files:
        try:
            filename = clean_pdf_filename(upload.filename)
            data = _read_pdf(upload)
            pages = count_pages(data)
            key = build_key(folder, sub, filename)
            put_object(bucket, key, data, content_type=PDF_CONTENT_TYPE)
            replaced = _register(
                db,
                bucket=bucket,
                key=key,
                filename=filename,
                size=len(data),
                pages=pages,
            )
            db.commit()
        except InvalidUploadFile as exc:
            outcomes.append(
                UploadOutcome(filename=upload.filename or "(unnamed)", error=str(exc))
            )
        except Exception as exc:  # a storage or DB failure for this one file
            db.rollback()
            outcomes.append(
                UploadOutcome(filename=upload.filename or "(unnamed)", error=str(exc))
            )
        else:
            outcomes.append(
                UploadOutcome(
                    filename=filename,
                    key=key,
                    size_bytes=len(data),
                    pages=pages,
                    replaced=replaced,
                )
            )
    return outcomes
