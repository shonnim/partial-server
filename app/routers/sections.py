import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.documents import get_document
from app.services.sections import (
    MAX_SECTION_BATCH,
    build_queue,
    document_contents,
    run_sections,
)
from app.services.tree import (
    DEFAULT_FILE_PAGE,
    list_level,
    resolve_selection,
    root_totals,
)
from app.templating import templates

router = APIRouter(prefix="/sections", tags=["sections"])


def _document_or_404(db: Session, document_id: uuid.UUID):
    doc = get_document(db, document_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    return doc


@router.get("", response_class=HTMLResponse, name="sections_page")
def sections_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """The same corpus tree as Parsing, read for section state instead."""
    total, failed = root_totals(db)

    return templates.TemplateResponse(
        request,
        "sections/index.html",
        {
            "active_nav": "sections",
            "level": list_level(db),
            "total": total,
            "failed": failed,
            "max_batch": MAX_SECTION_BATCH,
        },
    )


@router.get("/nodes", response_class=HTMLResponse, name="sections_nodes")
def sections_nodes(
    request: Request,
    path: str = "",
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """One level of the tree, fetched when a folder is expanded."""
    level = list_level(db, path, file_offset=offset, file_limit=DEFAULT_FILE_PAGE)
    template = (
        "sections/_partials/files.html" if offset else "sections/_partials/level.html"
    )

    return templates.TemplateResponse(request, template, {"level": level})


@router.post("/run", response_class=HTMLResponse, name="sections_run")
def sections_run(
    request: Request,
    selection: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Turn the selection into a queue the browser walks one document at a time.

    Errors come back as a rendered fragment rather than an HTTP error: the
    caller is a button on the page, and a 4xx would leave the panel blank.
    """
    doc_ids, error = resolve_selection(db, selection, limit=MAX_SECTION_BATCH)
    if error:
        return templates.TemplateResponse(
            request, "sections/_partials/queue.html", {"error": error}
        )

    return templates.TemplateResponse(
        request,
        "sections/_partials/queue.html",
        {"queue": build_queue(db, doc_ids)},
    )


@router.post(
    "/{document_id}/run", response_class=HTMLResponse, name="sections_run_one"
)
def sections_run_one(
    request: Request, document_id: uuid.UUID, db: Session = Depends(get_db)
) -> HTMLResponse:
    """File one document's blocks under its headers and report it as a row.

    One document per request, by design: every image in it is a model call,
    so a document with many figures is minutes, and the browser driving the
    queue is what keeps a batch from being one long HTTP request.
    """
    doc = _document_or_404(db, document_id)

    return templates.TemplateResponse(
        request,
        "sections/_partials/row_status.html",
        {"result": run_sections(db, doc)},
    )


@router.get(
    "/{document_id}/contents",
    response_class=HTMLResponse,
    name="sections_contents",
)
def sections_contents(
    request: Request,
    document_id: uuid.UUID,
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """The blocks a Section run stored for one document, a page at a time.

    The first page comes back as the pop-up; every page after it as the rows
    alone, which the "load more" button appends in place.
    """
    doc = _document_or_404(db, document_id)
    page = document_contents(db, doc.id, offset=offset)
    template = (
        "sections/_partials/contents_rows.html"
        if offset
        else "sections/_partials/contents_modal.html"
    )

    return templates.TemplateResponse(request, template, {"doc": doc, "page": page})
