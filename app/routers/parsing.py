import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.mineru import BACKEND, MineruError, health
from app.services.documents import get_document
from app.services.parse_runs import (
    HEALTH_SECONDS,
    MAX_PARSE_BATCH,
    POLL_SECONDS,
    build_queue,
    poll,
    submit,
)
from app.services.tree import (
    DEFAULT_FILE_PAGE,
    list_level,
    resolve_selection,
    root_totals,
)
from app.templating import templates

router = APIRouter(prefix="/parsing", tags=["parsing"])


def _document_or_404(db: Session, document_id: uuid.UUID):
    doc = get_document(db, document_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    return doc


@router.get("", response_class=HTMLResponse, name="parsing_page")
def parsing_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """The parsing tree. Only the root level is resolved up front."""
    total, failed = root_totals(db)

    return templates.TemplateResponse(
        request,
        "parsing/index.html",
        {
            "active_nav": "parsing",
            "level": list_level(db),
            "total": total,
            "failed": failed,
            "max_batch": MAX_PARSE_BATCH,
            "poll_seconds": POLL_SECONDS,
            "health_seconds": HEALTH_SECONDS,
            "backend": BACKEND,
        },
    )


@router.get("/nodes", response_class=HTMLResponse, name="parsing_nodes")
def parsing_nodes(
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
    template = "parsing/_partials/files.html" if offset else "parsing/_partials/level.html"

    return templates.TemplateResponse(request, template, {"level": level})


@router.get("/health", response_class=HTMLResponse, name="parsing_health")
def parsing_health(request: Request) -> HTMLResponse:
    """The parsing service's own health report, as the status box.

    Errors render as the box too — the caller is a button beside it, and a
    5xx would leave the panel blank rather than say the service is down.
    """
    try:
        report = health()
    except MineruError as exc:
        return templates.TemplateResponse(
            request, "parsing/_partials/health.html", {"error": str(exc)}
        )
    return templates.TemplateResponse(
        request, "parsing/_partials/health.html", {"health": report}
    )


@router.post("/run", response_class=HTMLResponse, name="parsing_run")
def parsing_run(
    request: Request,
    selection: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Turn the selection into a queue the browser walks one document at a time."""
    doc_ids, error = resolve_selection(db, selection, limit=MAX_PARSE_BATCH)
    if error:
        return templates.TemplateResponse(
            request, "parsing/_partials/queue.html", {"error": error}
        )

    return templates.TemplateResponse(
        request,
        "parsing/_partials/queue.html",
        {"queue": build_queue(db, doc_ids), "poll_seconds": POLL_SECONDS},
    )


@router.post(
    "/{document_id}/submit", response_class=HTMLResponse, name="parsing_submit"
)
def parsing_submit(
    request: Request, document_id: uuid.UUID, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Send one document to the parsing service and report the task as a row."""
    doc = _document_or_404(db, document_id)

    return templates.TemplateResponse(
        request,
        "parsing/_partials/row_status.html",
        {"step": submit(db, doc), "poll_seconds": POLL_SECONDS},
    )


@router.post("/{document_id}/poll", response_class=HTMLResponse, name="parsing_poll")
def parsing_poll(
    request: Request,
    document_id: uuid.UUID,
    task_id: str = Form(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Check one document's task; store the output once it has completed."""
    doc = _document_or_404(db, document_id)

    return templates.TemplateResponse(
        request,
        "parsing/_partials/row_status.html",
        {"step": poll(db, doc, task_id), "poll_seconds": POLL_SECONDS},
    )
