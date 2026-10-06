import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.document import Document
from app.schemas.document import DocumentBulkIn, DocumentBulkOut
from app.services.documents import (
    DuplicateIdsInBatch,
    get_document,
    upsert_documents,
)
from app.services.parse_artifacts import (
    ArtifactKind,
    ArtifactUnavailable,
    ParseArtifact,
    load_artifact_text,
    resolve_artifact,
    resolve_image,
)
from app.services.tree import DEFAULT_FILE_PAGE, list_level, root_totals
from app.storage import ObjectNotFound, open_object
from app.templating import templates

router = APIRouter(prefix="/documents", tags=["documents"])


@router.post("", response_model=DocumentBulkOut, status_code=status.HTTP_201_CREATED)
def create_documents(
    payload: DocumentBulkIn, db: Session = Depends(get_db)
) -> DocumentBulkOut:
    """Register a batch of documents, refreshing any id that already exists."""
    try:
        received = upsert_documents(db, payload.data)
    except DuplicateIdsInBatch as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"message": str(exc), "ids": exc.ids},
        ) from exc

    return DocumentBulkOut(received=received)


@router.get("", response_class=HTMLResponse, name="documents_page")
def documents_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """The corpus tree. Only the root level is resolved up front."""
    total, failed = root_totals(db)

    return templates.TemplateResponse(
        request,
        "documents/index.html",
        {
            "active_nav": "documents",
            "level": list_level(db),
            "total": total,
            "failed": failed,
        },
    )


@router.get("/nodes", response_class=HTMLResponse, name="documents_nodes")
def documents_nodes(
    request: Request,
    path: str = "",
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """One level of the tree, fetched when a folder is expanded.

    A non-zero offset is the "load more files" case, which returns file rows
    alone — the subfolders are already on the page.
    """
    level = list_level(db, path, file_offset=offset, file_limit=DEFAULT_FILE_PAGE)
    template = (
        "documents/_partials/files.html" if offset else "documents/_partials/level.html"
    )

    return templates.TemplateResponse(request, template, {"level": level})


@router.get("/{document_id}/viewer", response_class=HTMLResponse, name="document_viewer")
def document_viewer(
    request: Request, document_id: uuid.UUID, db: Session = Depends(get_db)
) -> HTMLResponse:
    doc = get_document(db, document_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")

    # The list opens this in a modal; the same URL still serves a full page so
    # the viewer stays deep-linkable and survives a ctrl-click into a new tab.
    template = (
        "documents/_partials/viewer_modal.html"
        if request.headers.get("hx-request")
        else "documents/viewer.html"
    )
    return templates.TemplateResponse(
        request, template, {"active_nav": "documents", "doc": doc}
    )


@router.get("/{document_id}/file", name="document_file")
def document_file(
    document_id: uuid.UUID, db: Session = Depends(get_db)
) -> StreamingResponse:
    """Stream the source PDF out of RustFS.

    The blob is proxied rather than handed over as a presigned URL so the object
    store stays unreachable from the browser.
    """
    doc = get_document(db, document_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")

    try:
        obj = open_object(doc.bucket, doc.s3_key)
    except ObjectNotFound as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "object missing from storage"
        ) from exc

    # Filenames are frequently Korean, so the name goes out RFC 5987-encoded;
    # a raw UTF-8 header value is not valid latin-1 and would fail to send.
    headers = {
        "Content-Disposition": f"inline; filename*=UTF-8''{quote(doc.filename)}",
    }
    if obj.content_length is not None:
        headers["Content-Length"] = str(obj.content_length)

    return StreamingResponse(
        obj.chunks,
        media_type=doc.content_type or obj.content_type,
        headers=headers,
    )


def _artifact_or_404(
    db: Session, document_id: uuid.UUID, kind: ArtifactKind
) -> tuple[Document, ParseArtifact]:
    """The document and its parse output, or a 404 naming what is missing."""
    doc = get_document(db, document_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")

    try:
        return doc, resolve_artifact(db, doc, kind)
    except ArtifactUnavailable as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.get("/{document_id}/parsed/images/{name}", name="document_parsed_image")
def document_parsed_image(
    document_id: uuid.UUID,
    name: str,
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """Stream one image of a parse run out of RustFS.

    Declared ahead of the ``{kind}`` routes so ``images`` is never read as a
    kind. The images are content-addressed, so a browser may keep one for a
    while: the same name never comes to mean a different picture.
    """
    doc = get_document(db, document_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")

    try:
        image = resolve_image(db, doc, name)
    except ArtifactUnavailable as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    try:
        obj = open_object(image.bucket, image.key)
    except ObjectNotFound as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"no image named {name} in this parse run"
        ) from exc

    headers = {
        "Content-Disposition": f"inline; filename*=UTF-8''{quote(image.filename)}",
        "Cache-Control": "private, max-age=3600",
    }
    if obj.content_length is not None:
        headers["Content-Length"] = str(obj.content_length)

    return StreamingResponse(obj.chunks, media_type=image.media_type, headers=headers)


@router.get(
    "/{document_id}/parsed/{kind}/viewer",
    response_class=HTMLResponse,
    name="document_parsed_viewer",
)
def document_parsed_viewer(
    request: Request,
    document_id: uuid.UUID,
    kind: ArtifactKind,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """The parsed markdown or JSON for one document, rendered as text."""
    doc, artifact = _artifact_or_404(db, document_id, kind)

    # Same split as the PDF viewer: a fragment for the pop-up, a full page for
    # a deep link or a ctrl-click.
    template = (
        "documents/_partials/parsed_modal.html"
        if request.headers.get("hx-request")
        else "documents/parsed.html"
    )
    return templates.TemplateResponse(
        request,
        template,
        {
            "active_nav": "documents",
            "doc": doc,
            "text": load_artifact_text(artifact),
        },
    )


@router.get("/{document_id}/parsed/{kind}", name="document_parsed_file")
def document_parsed_file(
    document_id: uuid.UUID,
    kind: ArtifactKind,
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """Stream a parse output straight out of RustFS, unformatted."""
    _doc, artifact = _artifact_or_404(db, document_id, kind)

    try:
        obj = open_object(artifact.bucket, artifact.key)
    except ObjectNotFound as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "object missing from storage"
        ) from exc

    headers = {
        "Content-Disposition": f"inline; filename*=UTF-8''{quote(artifact.filename)}",
    }
    if obj.content_length is not None:
        headers["Content-Length"] = str(obj.content_length)

    return StreamingResponse(obj.chunks, media_type=artifact.media_type, headers=headers)
