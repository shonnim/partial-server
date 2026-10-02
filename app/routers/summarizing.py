import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.documents import get_document
from app.services.summarizing import (
    build_queue,
    decorate_level,
    document_summaries,
    summarize_document,
)
from app.services.tree import (
    DEFAULT_FILE_PAGE,
    list_level,
    resolve_selection,
    root_totals,
)
from app.templating import templates

router = APIRouter(prefix="/summarizing", tags=["summarizing"])

# Documents one Summarize click may queue, walked one per request by the
# browser: every header is a model call, so a batch must never be one
# long HTTP request.
MAX_SUMMARIZE_BATCH = 200


def _document_or_404(db: Session, document_id: uuid.UUID):
    doc = get_document(db, document_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    return doc


@router.get("", response_class=HTMLResponse, name="summarizing_page")
def summarizing_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """The same corpus tree, read for summary state instead."""
    total, failed = root_totals(db)

    return templates.TemplateResponse(
        request,
        "summarizing/index.html",
        {
            "active_nav": "summarizing",
            "total": total,
            "failed": failed,
            "max_batch": MAX_SUMMARIZE_BATCH,
            **decorate_level(db, list_level(db)),
        },
    )


@router.get("/nodes", response_class=HTMLResponse, name="summarizing_nodes")
def summarizing_nodes(
    request: Request,
    path: str = "",
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """One level of the tree, fetched when a folder is expanded."""
    level = list_level(db, path, file_offset=offset, file_limit=DEFAULT_FILE_PAGE)
    template = (
        "summarizing/_partials/files.html"
        if offset
        else "summarizing/_partials/level.html"
    )

    return templates.TemplateResponse(request, template, decorate_level(db, level))


@router.post("/run", response_class=HTMLResponse, name="summarizing_run")
def summarizing_run(
    request: Request,
    selection: str = Form(""),
    force: bool = Form(False),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Turn the selection into a queue the browser walks one document at a time.

    Errors come back as a rendered fragment rather than an HTTP error: the
    caller is a button on the page, and a 4xx would leave the panel blank.
    """
    doc_ids, error = resolve_selection(db, selection, limit=MAX_SUMMARIZE_BATCH)
    if error:
        return templates.TemplateResponse(
            request, "summarizing/_partials/queue.html", {"error": error}
        )

    return templates.TemplateResponse(
        request,
        "summarizing/_partials/queue.html",
        {"queue": build_queue(db, doc_ids, force=force), "force": force},
    )


@router.post(
    "/{document_id}/run", response_class=HTMLResponse, name="summarizing_run_one"
)
def summarizing_run_one(
    request: Request,
    document_id: uuid.UUID,
    force: bool = Form(False),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Summarize one document's headers.

    One document per request, by design: every header is a model call, and
    the browser driving the queue is what keeps a batch from being one long
    HTTP request.
    """
    doc = _document_or_404(db, document_id)
    result = summarize_document(db, doc, force=force)

    return templates.TemplateResponse(
        request, "summarizing/_partials/row_status.html", {"result": result}
    )


@router.get(
    "/{document_id}/summaries", response_class=HTMLResponse, name="summarizing_summaries"
)
def summarizing_summaries(
    request: Request, document_id: uuid.UUID, db: Session = Depends(get_db)
) -> HTMLResponse:
    """The summaries stored for one document, as the pop-up the badge opens."""
    doc = _document_or_404(db, document_id)

    return templates.TemplateResponse(
        request,
        "summarizing/_partials/summaries_modal.html",
        {"doc": doc, "headers": document_summaries(db, doc.id)},
    )
