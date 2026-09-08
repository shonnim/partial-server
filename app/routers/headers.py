import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.documents import get_document
from app.services.header_extraction import (
    ExtractionError,
    ExtractionMethod,
    document_headers,
    extract_headers,
    header_count,
    prepare_for_extraction,
    save_headers,
)
from app.services.header_pages import (
    DEFAULT_PAGES,
    MAX_PAGES,
    MIN_PAGES,
    run_pages,
)
from app.services.headers import MAX_CHECK_BATCH, check_has_header, load_content_list
from app.services.tree import (
    DEFAULT_FILE_PAGE,
    list_level,
    resolve_selection,
    root_totals,
)
from app.templating import templates

router = APIRouter(prefix="/headers", tags=["headers"])


def _result(request: Request, context: dict) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "headers/_partials/result.html", context
    )


def _resolve_selection(
    db: Session, raw: str
) -> tuple[list[uuid.UUID] | None, str | None]:
    return resolve_selection(db, raw, limit=MAX_CHECK_BATCH)


def _document_or_404(db: Session, document_id: uuid.UUID):
    doc = get_document(db, document_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    return doc


@router.get("", response_class=HTMLResponse, name="headers_page")
def headers_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """The same corpus tree as Parsing, read for header state instead."""
    total, failed = root_totals(db)

    return templates.TemplateResponse(
        request,
        "headers/index.html",
        {
            "active_nav": "headers",
            "level": list_level(db),
            "total": total,
            "failed": failed,
            "max_batch": MAX_CHECK_BATCH,
            "methods": list(ExtractionMethod),
            "default_pages": DEFAULT_PAGES,
            "min_pages": MIN_PAGES,
            "max_pages": MAX_PAGES,
        },
    )


@router.get("/nodes", response_class=HTMLResponse, name="headers_nodes")
def headers_nodes(
    request: Request,
    path: str = "",
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """One level of the tree, fetched when a folder is expanded."""
    level = list_level(db, path, file_offset=offset, file_limit=DEFAULT_FILE_PAGE)
    template = (
        "headers/_partials/files.html" if offset else "headers/_partials/level.html"
    )

    return templates.TemplateResponse(request, template, {"level": level})


@router.post("/check", response_class=HTMLResponse, name="headers_check")
def headers_check(
    request: Request,
    selection: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Decide has_header for the selected documents that are still UNCHECKED.

    Errors come back as a rendered fragment rather than an HTTP error: the
    caller is a button on the page, and a 4xx would leave the panel blank.
    """
    doc_ids, error = _resolve_selection(db, selection)
    if error:
        return _result(request, {"error": error})

    return _result(request, {"report": check_has_header(db, doc_ids)})


@router.post("/get", response_class=HTMLResponse, name="headers_get")
def headers_get(
    request: Request,
    selection: str = Form(""),
    method: str = Form(ExtractionMethod.MANUAL.value),
    pages: int = Form(DEFAULT_PAGES),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Open extraction on a selection: check first, skip NO, queue the rest.

    The has_header gate runs here rather than inside each method, so every
    method that follows starts from the same guarantee: the document is known
    to have a contents page.

    Under the Pages method the queue that comes back is live: the page walks
    it one document at a time through the per-document route below.
    """
    try:
        chosen = ExtractionMethod(method)
    except ValueError:
        return _result(request, {"error": f"Unknown method {method!r}."})

    if chosen is ExtractionMethod.PAGES and not MIN_PAGES <= pages <= MAX_PAGES:
        return _result(
            request, {"error": f"Pages must be between {MIN_PAGES} and {MAX_PAGES}."}
        )

    doc_ids, error = _resolve_selection(db, selection)
    if error:
        return _result(request, {"error": error})

    return templates.TemplateResponse(
        request,
        "headers/_partials/gate_result.html",
        {"gate": prepare_for_extraction(db, doc_ids, chosen), "pages": pages},
    )


@router.post(
    "/{document_id}/pages",
    response_class=HTMLResponse,
    name="header_pages_run",
)
def header_pages_run(
    request: Request,
    document_id: uuid.UUID,
    pages: int = Form(DEFAULT_PAGES),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Run the Pages method on one document and report the outcome as a row.

    One document per request, by design: the queue on the page calls this once
    per eligible document, so a slow parse holds up one row rather than the
    whole batch. Failures render as a row too — the caller is a status cell,
    and a 4xx would leave it blank.
    """
    doc = _document_or_404(db, document_id)

    return templates.TemplateResponse(
        request,
        "headers/_partials/pages_row.html",
        {"result": run_pages(db, doc, pages)},
    )


@router.get(
    "/{document_id}/manual",
    response_class=HTMLResponse,
    name="header_manual_form",
)
def header_manual_form(
    request: Request, document_id: uuid.UUID, db: Session = Depends(get_db)
) -> HTMLResponse:
    """The paste-a-contents-page dialog for one document."""
    doc = _document_or_404(db, document_id)

    return templates.TemplateResponse(
        request,
        "headers/_partials/manual_modal.html",
        {"doc": doc, "existing": header_count(db, doc.id), "contents": ""},
    )


@router.post(
    "/{document_id}/manual",
    response_class=HTMLResponse,
    name="header_manual_submit",
)
def header_manual_submit(
    request: Request,
    document_id: uuid.UUID,
    contents: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Turn the pasted text into headers and store them.

    A failure re-renders the dialog with the text still in it — the paste is
    the expensive part for whoever is doing this, and losing it to an error
    message would be the worst part of the whole screen.
    """
    doc = _document_or_404(db, document_id)

    try:
        extracted = extract_headers(contents)
    except ExtractionError as exc:
        return templates.TemplateResponse(
            request,
            "headers/_partials/manual_modal.html",
            {
                "doc": doc,
                "existing": header_count(db, doc.id),
                "contents": contents,
                "error": str(exc),
            },
        )

    # The page indexes come from the content list a parse left behind. A
    # document with no readable content list still gets its headers; only the
    # two index columns stay NULL.
    blocks, _ = load_content_list(db, doc)
    saved = save_headers(db, doc.id, extracted, blocks)

    return templates.TemplateResponse(
        request,
        "headers/_partials/outline_modal.html",
        {
            "doc": doc,
            "roots": document_headers(db, doc.id),
            "saved": saved,
            "total": saved,
        },
    )


@router.get(
    "/{document_id}/outline",
    response_class=HTMLResponse,
    name="header_outline",
)
def header_outline(
    request: Request, document_id: uuid.UUID, db: Session = Depends(get_db)
) -> HTMLResponse:
    """The headers stored for one document, as a tree."""
    doc = _document_or_404(db, document_id)

    return templates.TemplateResponse(
        request,
        "headers/_partials/outline_modal.html",
        {
            "doc": doc,
            "roots": document_headers(db, doc.id),
            "saved": None,
            "total": header_count(db, doc.id),
        },
    )
