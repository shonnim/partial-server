"""The folder tree over document object keys.

The corpus is ~9k documents, so the tree is never materialised whole: one
request resolves exactly one level, and the UI asks for the next level when a
folder is expanded. Folder counts are aggregates over everything beneath a
folder, computed in Postgres rather than by walking rows in Python.

Folders come from the object key rather than the ``folder_path`` column while
that column is still unpopulated; the two expressions agree once it is
backfilled.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from math import ceil

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.document import DocumentStatus

# Files listed per folder before the UI has to ask for more.
DEFAULT_FILE_PAGE = 50

# Everything in an object key before the last slash. A key with no slash is a
# document at the root of the bucket and has no folder at all.
_FOLDER_EXPR = "substring(s3_key from '^(.*)/[^/]*$')"


@dataclass(frozen=True)
class FolderNode:
    """A folder, with counts covering every document beneath it."""

    name: str
    path: str
    total: int
    failed: int


@dataclass(frozen=True)
class FileNode:
    """One document sitting directly in the folder being listed."""

    id: uuid.UUID
    filename: str
    size_bytes: int
    pages: int | None
    status: DocumentStatus


@dataclass(frozen=True)
class Level:
    """One expanded level of the tree: its subfolders and its own files."""

    path: str
    folders: Sequence[FolderNode]
    files: Sequence[FileNode]
    file_total: int
    file_offset: int
    file_limit: int

    @property
    def remaining_files(self) -> int:
        return max(0, self.file_total - (self.file_offset + len(self.files)))

    @property
    def next_offset(self) -> int:
        return self.file_offset + len(self.files)

    @property
    def is_empty(self) -> bool:
        return not self.folders and not self.files


def _depth(path: str) -> int:
    """Number of segments in a folder path; 0 for the root."""
    return len(path.split("/")) if path else 0


def child_folders(db: Session, path: str) -> list[FolderNode]:
    """Folders directly inside ``path``, with recursive document counts.

    ``starts_with`` rather than LIKE: folder names here contain underscores and
    brackets, and an underscore is a LIKE wildcard that would over-match.
    """
    child_depth = _depth(path) + 1

    rows = db.execute(
        text(f"""
            WITH scoped AS (
                SELECT status, {_FOLDER_EXPR} AS folder
                FROM documents
                WHERE :path = '' OR starts_with(s3_key, :path || '/')
            )
            SELECT split_part(folder, '/', :child_depth) AS name,
                   count(*) AS total,
                   count(*) FILTER (WHERE status = 'FAILED') AS failed
            FROM scoped
            WHERE folder IS NOT NULL
              AND split_part(folder, '/', :child_depth) <> ''
            GROUP BY 1
            ORDER BY 1
        """),
        {"path": path, "child_depth": child_depth},
    ).all()

    return [
        FolderNode(
            name=r.name,
            path=f"{path}/{r.name}" if path else r.name,
            total=r.total,
            failed=r.failed,
        )
        for r in rows
    ]


def folder_files(
    db: Session,
    path: str,
    *,
    offset: int = 0,
    limit: int = DEFAULT_FILE_PAGE,
) -> tuple[list[FileNode], int]:
    """Documents sitting directly in ``path``, plus how many there are in total."""
    offset = max(0, offset)
    limit = max(1, min(limit, 500))

    # A document at the bucket root has a NULL folder, which no equality test
    # would match, so the root is asked for explicitly.
    predicate = f"{_FOLDER_EXPR} IS NULL" if not path else f"{_FOLDER_EXPR} = :path"

    total = db.execute(
        text(f"SELECT count(*) FROM documents WHERE {predicate}"), {"path": path}
    ).scalar_one()

    rows = db.execute(
        text(f"""
            SELECT id, filename, size_bytes, pages, status
            FROM documents
            WHERE {predicate}
            ORDER BY filename, id
            LIMIT :limit OFFSET :offset
        """),
        {"path": path, "limit": limit, "offset": offset},
    ).all()

    files = [
        FileNode(
            id=r.id,
            filename=r.filename,
            size_bytes=r.size_bytes,
            pages=r.pages,
            status=DocumentStatus(r.status),
        )
        for r in rows
    ]
    return files, total


def list_level(
    db: Session,
    path: str = "",
    *,
    file_offset: int = 0,
    file_limit: int = DEFAULT_FILE_PAGE,
) -> Level:
    """Resolve one level of the tree — never the whole corpus."""
    folders = child_folders(db, path) if file_offset == 0 else []
    files, total = folder_files(db, path, offset=file_offset, limit=file_limit)

    return Level(
        path=path,
        folders=folders,
        files=files,
        file_total=total,
        file_offset=file_offset,
        file_limit=file_limit,
    )


def root_totals(db: Session) -> tuple[int, int]:
    """Corpus-wide (total, failed), for the tree header."""
    r = db.execute(
        text("""
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE status = 'FAILED') AS failed
            FROM documents
        """)
    ).one()
    return r.total, r.failed


def page_count(total: int, per_page: int) -> int:
    return max(1, ceil(total / per_page)) if total else 1
