"""Migrating documents into the RAG files bucket.

The Transform page assembles, for each picked document, one self-contained
folder in the RAG bucket that downstream consumers can read without this
server: the source PDF, a small metadata JSON, and the latest parse outputs
(content list and page-crop images) copied out of the derived bucket.

Layout written for a document whose key is ``{folder}/{uuid}.pdf`` and whose
filename is ``{stem}.pdf``:

    {folder}/{stem}/{stem}.pdf
    {folder}/{stem}/{stem}_metadata.json
    {folder}/{stem}/{stem}_content_list.json
    {folder}/{stem}/images/{name}.jpg

Everything but the metadata JSON is a server-side copy — no PDF bytes pass
through this process. Copies are idempotent: re-migrating a document rewrites
the same keys with the same content, so a second click cannot corrupt
anything.
"""

import json
import posixpath
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.document import Document
from app.services.parse_artifacts import IMAGES_FOLDER, latest_parsed_doc, run_prefix
from app.storage import copy_object, list_all_objects, put_object

# Documents one Migrate click may move. Each document is one PDF copy, one
# JSON put, and as many image copies as the parse produced figures, so the
# cap stays below the tree-selection caps elsewhere.
MAX_TRANSFORM_BATCH = 200

# The content list copied into the folder, best variant first — the same
# preference order the parse-artifact panel uses.
_CONTENT_LIST_SUFFIXES = ("_content_list.json", "_content_list_v2.json")


@dataclass(frozen=True)
class DocumentMigration:
    """What migrating one document did, or why it could not be done."""

    doc_id: uuid.UUID
    filename: str
    dest_prefix: str
    # Objects now sitting under dest_prefix because of this call.
    copied: int = 0
    images: int = 0
    content_list: str | None = None
    # Why the parse outputs are absent — the PDF and metadata still migrated.
    missing: str | None = None
    # Why the document migrated partially or not at all.
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def complete(self) -> bool:
        """Whether the migrated folder has everything, content list included.

        Only a complete migration stamps ``migrated_at`` and takes the row out
        of the tree selection — a document migrated before it was parsed stays
        selectable, so re-running it later finishes the folder. A parse run
        with no images is still complete: a document may simply have no
        figures.
        """
        return self.ok and self.content_list is not None


@dataclass(frozen=True)
class TransformResult:
    """One Migrate click, document by document."""

    bucket: str
    migrations: Sequence[DocumentMigration] = field(default_factory=tuple)

    @property
    def succeeded(self) -> int:
        return sum(1 for m in self.migrations if m.ok)

    @property
    def failed(self) -> int:
        return len(self.migrations) - self.succeeded

    @property
    def completed_ids(self) -> list[uuid.UUID]:
        """The documents now stamped as migrated, for the tree to disable."""
        return [m.doc_id for m in self.migrations if m.complete]


def dest_prefix_for(document: Document) -> str:
    """The folder this document migrates into, inside the RAG bucket.

    The folder of the object key (never the user-facing ``folder_path``
    column, which may be unpopulated) plus a directory named after the
    filename's stem.
    """
    folder = posixpath.dirname(document.s3_key)
    stem = posixpath.splitext(document.filename)[0]
    return f"{folder}/{stem}/" if folder else f"{stem}/"


def _metadata_bytes(document: Document) -> bytes:
    """The metadata JSON: id, display filename, page count.

    Underscores in the stored filename stand in for spaces, so the display
    name swaps them back.
    """
    payload = {
        "id": str(document.id),
        "filename": document.filename.replace("_", " "),
        "pages": document.pages,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def _migrate_one(db: Session, document: Document, bucket: str) -> DocumentMigration:
    """Copy one document's folder into the RAG bucket.

    The PDF and metadata always go; the parse outputs go when the document
    has a parse run to read from. Anything missing is named in the result —
    a folder without its content list should never look complete.
    """
    prefix = dest_prefix_for(document)
    stem = posixpath.splitext(document.filename)[0]
    copied = 0

    copy_object(document.bucket, document.s3_key, bucket, f"{prefix}{document.filename}")
    copied += 1

    put_object(
        bucket,
        f"{prefix}{stem}_metadata.json",
        _metadata_bytes(document),
        content_type="application/json; charset=utf-8",
    )
    copied += 1

    parsed = latest_parsed_doc(db, document.id)
    if parsed is None or not parsed.bucket:
        return DocumentMigration(
            doc_id=document.id,
            filename=document.filename,
            dest_prefix=prefix,
            copied=copied,
            missing="no parse run — content list and images not copied",
        )

    source_prefix = run_prefix(parsed, document)
    objects = list_all_objects(parsed.bucket, source_prefix)

    content_key = next(
        (
            o.key
            for suffix in _CONTENT_LIST_SUFFIXES
            for o in objects
            if o.key.endswith(suffix)
        ),
        None,
    )
    if content_key is not None:
        copy_object(
            parsed.bucket, content_key, bucket, f"{prefix}{posixpath.basename(content_key)}"
        )
        copied += 1

    images = 0
    image_prefix = f"{source_prefix}{IMAGES_FOLDER}/"
    for obj in objects:
        if not obj.key.startswith(image_prefix):
            continue
        copy_object(
            parsed.bucket,
            obj.key,
            bucket,
            f"{prefix}{IMAGES_FOLDER}/{posixpath.basename(obj.key)}",
        )
        images += 1
    copied += images

    missing = None
    if content_key is None:
        missing = f"no content list under {parsed.bucket}/{source_prefix}"
    elif images == 0:
        missing = "parse run has no images"

    return DocumentMigration(
        doc_id=document.id,
        filename=document.filename,
        dest_prefix=prefix,
        copied=copied,
        images=images,
        content_list=posixpath.basename(content_key) if content_key else None,
        missing=missing,
    )


def migrate_documents(db: Session, doc_ids: Sequence[uuid.UUID]) -> TransformResult:
    """Migrate every named document into the RAG bucket, one at a time.

    A document that fails is recorded with its error and the batch moves on:
    copies are idempotent, so re-running the selection finishes whatever a
    partial failure left behind.
    """
    bucket = get_settings().bucket_rag_files
    documents = db.scalars(
        select(Document).where(Document.id.in_(list(doc_ids)))
    ).all()
    by_id = {d.id: d for d in documents}

    migrations: list[DocumentMigration] = []
    for doc_id in doc_ids:
        document = by_id.get(doc_id)
        if document is None:
            continue  # the selection outlived the row; nothing to report on
        try:
            migrations.append(_migrate_one(db, document, bucket))
        except Exception as exc:  # noqa: BLE001 — recorded per document, never silent
            migrations.append(
                DocumentMigration(
                    doc_id=document.id,
                    filename=document.filename,
                    dest_prefix=dest_prefix_for(document),
                    error=f"{type(exc).__name__}: {exc}",
                )
            )

    result = TransformResult(bucket=bucket, migrations=migrations)

    # Postgres is the source of truth for migration state: the stamp is what
    # the Transform tree disables rows by, never the bucket's contents.
    if result.completed_ids:
        db.execute(
            update(Document)
            .where(Document.id.in_(result.completed_ids))
            .values(migrated_at=func.now(), updated_at=func.now())
        )
        db.commit()

    return result
