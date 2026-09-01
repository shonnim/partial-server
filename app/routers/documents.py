import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.document import DocumentBulkIn, DocumentBulkOut
from app.services.documents import (
    DEFAULT_PAGE_SIZE,
    DuplicateIdsInBatch,
    get_document,
    list_categories,
    list_documents,
    upsert_documents,
)
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


def _list_context(
    db: Session,
    page: int,
    q: str | None,
    category: str | None,
    *,
    fragment: bool,
) -> dict[str, object]:
    """Everything both the full page and the HTMX fragment need to render."""
    return {
        "active_nav": "documents",
        "fragment": fragment,
        "page": list_documents(
            db, page=page, page_size=DEFAULT_PAGE_SIZE, q=q, category=category
        ),
        "categories": list_categories(db),
        "q": q,
        "category": category,
    }


@router.get("", response_class=HTMLResponse, name="documents_page")
def documents_page(
    request: Request,
    page: int = Query(1, ge=1),
    q: str | None = None,
    category: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "documents/list.html",
        _list_context(db, page, q, category, fragment=False),
    )


@router.get("/table", response_class=HTMLResponse, name="documents_table")
def documents_table(
    request: Request,
    page: int = Query(1, ge=1),
    q: str | None = None,
    category: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """The table fragment, swapped in when a filter or the pager changes."""
    return templates.TemplateResponse(
        request,
        "documents/_partials/table.html",
        _list_context(db, page, q, category, fragment=True),
    )


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
