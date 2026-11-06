"""Matching source-system publication listings against documents in a folder.

The source system lists a 파일명 like "2026년 나라살림 예산개요.pdf" (sometimes
.hwp or another office format) while the uploaded file is named
"2009년도_성과계획서(산림청).pdf": the same name apart from separators and the
extension. Comparison therefore runs on a normalised form of both sides — NFC
unicode, document extension dropped, underscores read as spaces, whitespace
collapsed, case folded — never on the raw strings.

A 파일명 ending in .zip is special: the archive itself was never registered,
its PDFs were. Such an entry matches a *folder* named like the archive instead
of a file, and then stands for every document in that folder — they all get the
same 게시물ID · 첨부파일ID · 첨부순번, because those ids belong to the zip.
"""

import re
import unicodedata
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.schemas.mapping import MappingEntry

# A mapping run covers one publication folder, not the corpus. Anything larger
# than this is almost certainly a path typed one level too high.
MAX_FOLDER_DOCS = 2000

# A known list rather than "any trailing .xyz": names themselves can end in a
# dot-word ("… v1.2", "… 제2.5차"), and only a real extension may be dropped.
_EXTENSIONS = r"\.(pdf|hwpx?|docx?|xlsx?|pptx?|txt|zip)$"


def normalize_name(name: str) -> str:
    """The form filenames and 파일명 are compared in."""
    name = unicodedata.normalize("NFC", name).strip()
    name = re.sub(_EXTENSIONS, "", name, flags=re.IGNORECASE)
    name = name.replace("_", " ")
    name = re.sub(r"\s+", " ", name).strip()
    return name.casefold()


def is_archive_name(name: str) -> bool:
    """Whether a 파일명 names an archive rather than a registerable document.

    Only archive entries are allowed to match a folder: a mistyped PDF name
    must stay "not found", never silently bulk-write to a folder that happens
    to share the name.
    """
    return bool(re.search(r"\.zip$", name.strip(), flags=re.IGNORECASE))


@dataclass(frozen=True)
class FolderDoc:
    """One document beneath the checked folder."""

    id: uuid.UUID
    filename: str
    # The folder part of the object key ("" for a document at the bucket
    # root), which is what a zip entry's folder match runs on.
    folder: str = ""


@dataclass(frozen=True)
class EntryCheck:
    """The verdict on one pasted entry."""

    entry: MappingEntry
    # The single document the name resolved to, or None.
    doc: FolderDoc | None
    # How many documents in the folder share the normalised name. 0 is "not
    # found"; anything above 1 is ambiguous and deliberately left unresolved.
    candidates: int
    # Whether another pasted entry normalises to the same name, which would
    # make any later write ambiguous even when the file match is unique.
    duplicate: bool
    # A zip entry that matched no file may instead match the folder its PDFs
    # were extracted into. ``folder`` is that folder when exactly one matched,
    # ``folder_docs`` every document inside it (subfolders included), and
    # ``folder_candidates`` how many folders shared the name — above 1 is
    # ambiguous, same as for files.
    folder: str | None = None
    folder_docs: tuple[FolderDoc, ...] = ()
    folder_candidates: int = 0

    @property
    def ok(self) -> bool:
        resolved = self.doc is not None or bool(self.folder_docs)
        return resolved and not self.duplicate

    @property
    def resolved(self) -> tuple[FolderDoc, ...]:
        """Every document a save would write this entry's ids onto."""
        if self.doc is not None:
            return (self.doc,)
        return self.folder_docs


@dataclass(frozen=True)
class CheckResult:
    path: str
    total_docs: int
    checks: Sequence[EntryCheck]
    # Filenames in the folder that no pasted entry named.
    unmapped: Sequence[str]

    @property
    def matched(self) -> int:
        return sum(1 for c in self.checks if c.ok)

    @property
    def not_found(self) -> int:
        return sum(
            1
            for c in self.checks
            if c.candidates == 0 and c.folder_candidates == 0
        )

    @property
    def ambiguous(self) -> int:
        return sum(
            1
            for c in self.checks
            if c.candidates > 1 or c.folder_candidates > 1 or c.duplicate
        )


@dataclass(frozen=True)
class SaveResult:
    """A save is a check whose clean matches were written to the documents."""

    check: CheckResult
    saved: int


def folder_documents(db: Session, path: str) -> list[FolderDoc] | None:
    """Every document whose key starts with ``path``, or None when too many.

    The path is a prefix, so its last segment may be unfinished:
    "재정간행물/월간나라재정/2018년_" finds every folder whose name starts with
    "2018년_". A trailing slash pins the match to one exact folder.

    ``starts_with`` rather than LIKE, as everywhere else in this project:
    folder names contain underscores, which are LIKE wildcards.
    """
    rows = db.execute(
        text("""
            SELECT id, filename, s3_key
            FROM documents
            WHERE starts_with(s3_key, :path)
            ORDER BY filename, id
            LIMIT :limit
        """),
        {"path": path, "limit": MAX_FOLDER_DOCS + 1},
    ).all()

    if len(rows) > MAX_FOLDER_DOCS:
        return None
    return [
        FolderDoc(
            id=r.id,
            filename=r.filename,
            folder=r.s3_key.rsplit("/", 1)[0] if "/" in r.s3_key else "",
        )
        for r in rows
    ]


def check_mapping(
    db: Session, path: str, entries: Sequence[MappingEntry]
) -> tuple[CheckResult | None, str | None]:
    """Compare pasted entries against the folder. Returns (result, error)."""
    # Only the leading slash is noise: a trailing slash is meaningful, it pins
    # the prefix to an exact folder instead of matching unfinished names.
    path = path.strip().lstrip("/")
    if not path:
        return None, "Enter a folder path first."
    if not entries:
        return None, "The JSON holds no entries."

    docs = folder_documents(db, path)
    if docs is None:
        return None, (
            f"More than {MAX_FOLDER_DOCS:,} documents under “{path}”. "
            "Check one publication folder at a time."
        )
    if not docs:
        return None, f"No documents found under “{path}”."

    by_name: dict[str, list[FolderDoc]] = {}
    for doc in docs:
        by_name.setdefault(normalize_name(doc.filename), []).append(doc)

    # Every folder that exists under the path, ancestors included: a zip whose
    # files all landed in subfolders still has to resolve to the folder named
    # after it, even when no file sits directly inside that folder.
    folders: set[str] = set()
    for doc in docs:
        if not doc.folder:
            continue
        parts = doc.folder.split("/")
        for depth in range(1, len(parts) + 1):
            folders.add("/".join(parts[:depth]))
    folders_by_name: dict[str, list[str]] = {}
    for folder in sorted(folders):
        key = normalize_name(folder.rsplit("/", 1)[-1])
        folders_by_name.setdefault(key, []).append(folder)

    entry_names = Counter(normalize_name(e.name) for e in entries)

    checks: list[EntryCheck] = []
    taken: set[uuid.UUID] = set()
    for entry in entries:
        key = normalize_name(entry.name)
        found = by_name.get(key, [])

        # A file match always wins; the folder pass runs only for an archive
        # entry that matched no file at all.
        folder_matches: list[str] = []
        folder_docs: tuple[FolderDoc, ...] = ()
        if not found and is_archive_name(entry.name):
            folder_matches = folders_by_name.get(key, [])
            if len(folder_matches) == 1:
                match = folder_matches[0]
                folder_docs = tuple(
                    d
                    for d in docs
                    if d.folder == match or d.folder.startswith(match + "/")
                )

        check = EntryCheck(
            entry=entry,
            doc=found[0] if len(found) == 1 else None,
            candidates=len(found),
            duplicate=entry_names[key] > 1,
            folder=folder_matches[0] if len(folder_matches) == 1 else None,
            folder_docs=folder_docs,
            folder_candidates=len(folder_matches),
        )
        checks.append(check)
        taken.update(d.id for d in check.resolved)

    unmapped = [d.filename for d in docs if d.id not in taken]

    return (
        CheckResult(path=path, total_docs=len(docs), checks=checks, unmapped=unmapped),
        None,
    )


def save_mapping(
    db: Session, path: str, entries: Sequence[MappingEntry]
) -> tuple[SaveResult | None, str | None]:
    """Write each cleanly matched entry's ids onto its document.

    The check is re-run here rather than trusted from the browser, so Save is
    safe to click on stale state. Only unambiguous matches are written; the
    rest stay untouched and show up in the result as skipped. Re-saving the
    same JSON sets the same values again — idempotent by construction.
    """
    result, error = check_mapping(db, path, entries)
    if error:
        return None, error

    rows = [
        _save_params(doc.id, check.entry)
        for check in result.checks
        if check.ok
        for doc in check.resolved
    ]
    if rows:
        db.execute(_SAVE_SQL, rows)
        db.commit()

    return SaveResult(check=result, saved=len(rows)), None


def save_entry(db: Session, document_id: uuid.UUID, entry: MappingEntry) -> None:
    """Write one entry's ids onto one explicitly chosen document.

    The manual path: no name matching here — the person pointed at the row,
    which is exactly what this exists for when the automatic match cannot.
    """
    db.execute(_SAVE_SQL, _save_params(document_id, entry))
    db.commit()


_SAVE_SQL = text("""
    UPDATE documents
    SET source_document_id = :source_document_id,
        atch_file_id = :atch_file_id,
        atch_file_seq = :atch_file_seq,
        updated_at = now()
    WHERE id = :id
""")


def _save_params(document_id: uuid.UUID, entry: MappingEntry) -> dict:
    return {
        "id": document_id,
        "source_document_id": entry.source_document_id,
        "atch_file_id": entry.atch_file_id,
        "atch_file_seq": entry.atch_file_seq,
    }
