"""Finding and reading the text a parse run left in RustFS.

``parsed_docs`` records which bucket and which folder a run wrote into, but not
the individual object keys. The run directory is therefore derived — folder
plus the document's stem — and the keys *inside* it are discovered by listing,
never assumed. That way a change in the parser's file naming shows up as "no
artifact of this kind" rather than as a 404 on a key nobody ever wrote.

Layout a run produces, for a document named ``report.pdf``:

    {folder_path}/report/report.md
    {folder_path}/report/report_content_list.json
    {folder_path}/report/report_content_list_v2.json
    {folder_path}/report/images/{sha256}.jpg
"""

import enum
import json
import posixpath
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.document import Document
from app.models.parsed_doc import ParsedDoc
from app.storage import ObjectNotFound, list_objects, read_object


class ArtifactKind(str, enum.Enum):
    """The parse outputs the panel can show."""

    JSON = "json"
    MD = "md"


# How each kind is recognised among the keys in a run directory, best match
# first. The original content list is what the panel reviews; the v2 variant is
# only a fallback for runs that wrote nothing else.
_SUFFIXES: dict[ArtifactKind, tuple[str, ...]] = {
    ArtifactKind.JSON: ("_content_list.json", "_content_list_v2.json", ".json"),
    ArtifactKind.MD: (".md",),
}

_MEDIA_TYPES: dict[ArtifactKind, str] = {
    ArtifactKind.JSON: "application/json; charset=utf-8",
    ArtifactKind.MD: "text/markdown; charset=utf-8",
}

# Ceiling on what the browser is asked to render in one <pre>. Anything larger
# is offered as a download instead of being pushed through the page.
MAX_INLINE_BYTES = 8 * 1024 * 1024


class ArtifactUnavailable(LookupError):
    """This document has no parse artifact of the requested kind."""


@dataclass(frozen=True)
class ParseArtifact:
    """One text output of a parse run, located in RustFS."""

    kind: ArtifactKind
    bucket: str
    key: str
    size_bytes: int

    @property
    def filename(self) -> str:
        return posixpath.basename(self.key)

    @property
    def media_type(self) -> str:
        return _MEDIA_TYPES[self.kind]


@dataclass(frozen=True)
class ArtifactText:
    """An artifact prepared for display, or the reason it cannot be shown."""

    artifact: ParseArtifact
    body: str | None
    error: str | None
    #: Number of blocks in a content list. None for markdown, and for JSON that
    #: is not a top-level list — a count off some other shape would be a lie.
    blocks: int | None = None


def latest_parsed_doc(db: Session, doc_id: uuid.UUID) -> ParsedDoc | None:
    """The most recent parse row for a document.

    Re-parsing appends a row rather than replacing one, so the highest id is
    the run whose output the panel should be showing.
    """
    return db.scalars(
        select(ParsedDoc)
        .where(ParsedDoc.doc_id == doc_id)
        .order_by(ParsedDoc.id.desc())
        .limit(1)
    ).first()


def run_prefix(parsed: ParsedDoc, document: Document) -> str:
    """The directory a run wrote into, as an object-key prefix."""
    stem = posixpath.splitext(document.filename)[0]
    folder = (parsed.folder_path or "").strip("/")
    return f"{folder}/{stem}/" if folder else f"{stem}/"


def resolve_artifact(
    db: Session, document: Document, kind: ArtifactKind
) -> ParseArtifact:
    """Locate one parse output for a document.

    Raises ArtifactUnavailable when the document has never been parsed, when
    the run recorded no bucket, or when the run directory holds nothing of
    this kind.
    """
    parsed = latest_parsed_doc(db, document.id)
    if parsed is None:
        raise ArtifactUnavailable(f"{document.filename} has not been parsed")
    if not parsed.bucket:
        raise ArtifactUnavailable(
            f"parse run {parsed.id} recorded no bucket to read from"
        )

    prefix = run_prefix(parsed, document)
    try:
        objects = list_objects(parsed.bucket, prefix)
    except ObjectNotFound as exc:
        raise ArtifactUnavailable(f"nothing stored at {parsed.bucket}/{prefix}") from exc

    for suffix in _SUFFIXES[kind]:
        for obj in objects:
            if obj.key.endswith(suffix):
                return ParseArtifact(
                    kind=kind,
                    bucket=parsed.bucket,
                    key=obj.key,
                    size_bytes=obj.size,
                )

    raise ArtifactUnavailable(f"no {kind.value} output under {parsed.bucket}/{prefix}")


def load_artifact_text(artifact: ParseArtifact) -> ArtifactText:
    """Fetch an artifact and prepare it for the browser.

    Every way this can go wrong — too large, not UTF-8, not valid JSON — comes
    back named in ``error`` rather than as an empty body, because an empty
    panel and a broken parse look identical to whoever is reviewing.
    """
    raw, oversized = read_object(
        artifact.bucket, artifact.key, max_bytes=MAX_INLINE_BYTES
    )
    if oversized:
        return ArtifactText(
            artifact=artifact,
            body=None,
            error="Too large to display here. Open the raw file instead.",
        )

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return ArtifactText(
            artifact=artifact,
            body=None,
            error=f"Not valid UTF-8 at byte {exc.start}.",
        )

    if artifact.kind is not ArtifactKind.JSON:
        return ArtifactText(artifact=artifact, body=text, error=None)

    # Pretty-print for review. Unparseable JSON is shown as it came out of the
    # parser, with the failure stated — that is the thing worth looking at.
    try:
        parsed_json = json.loads(text)
    except json.JSONDecodeError as exc:
        return ArtifactText(
            artifact=artifact,
            body=text,
            error=f"Invalid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno}).",
        )

    return ArtifactText(
        artifact=artifact,
        body=json.dumps(parsed_json, indent=2, ensure_ascii=False),
        error=None,
        blocks=len(parsed_json) if isinstance(parsed_json, list) else None,
    )
