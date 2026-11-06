import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.documents import get_document
from app.services.header_extraction import (
    DEFAULT_LLM,
    HEADER_LLMS,
    ExtractionError,
    ExtractionMethod,
    document_headers,
    extract_headers,
    header_count,
    parse_index_edits,
    prepare_for_extraction,
    resolve_llm,
    save_headers,
    update_page_indexes,
)
from app.services.header_pages import (
    DEFAULT_END_PAGE,
    DEFAULT_START_PAGE,
    MAX_PAGE_SPAN,
    range_error,
    run_group,
    run_pages,
)
from app.services.header_transfer import (
    TransferError,
    apply_headers_to_bucket,
    apply_toc,
    delete_toc_entry,
    diff_for_document,
    load_bucket_toc,
    parse_row_edits,
    update_bucket_toc,
    update_stored_headers,
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
            "llms": HEADER_LLMS,
            "default_start_page": DEFAULT_START_PAGE,
            "default_end_page": DEFAULT_END_PAGE,
            "max_page_span": MAX_PAGE_SPAN,
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
    start_page: int = Form(DEFAULT_START_PAGE),
    end_page: int = Form(DEFAULT_END_PAGE),
    force: bool = Form(False),
    llm: str = Form(DEFAULT_LLM),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Open extraction on a selection: check first, skip NO, queue the rest.

    The has_header gate runs here rather than inside each method, so every
    method that follows starts from the same guarantee: the document is known
    to have a contents page. ``force`` is the page's Force toggle: with it,
    documents whose headers are already stored go through the gate to be
    re-extracted instead of being held back.

    Under the Pages method the queue that comes back is live: the page walks
    it one document at a time through the per-document route below.
    """
    try:
        chosen = ExtractionMethod(method)
    except ValueError:
        return _result(request, {"error": f"Unknown method {method!r}."})

    if chosen is ExtractionMethod.PAGES:
        range_problem = range_error(start_page, end_page)
        if range_problem is not None:
            return _result(request, {"error": f"Page range: {range_problem}."})

    doc_ids, error = _resolve_selection(db, selection)
    if error:
        return _result(request, {"error": error})

    llm = resolve_llm(llm)
    return templates.TemplateResponse(
        request,
        "headers/_partials/gate_result.html",
        {
            "gate": prepare_for_extraction(db, doc_ids, chosen, force=force),
            "start_page": start_page,
            "end_page": end_page,
            "llm": llm,
            "llm_label": HEADER_LLMS[llm],
        },
    )


@router.post(
    "/{document_id}/pages",
    response_class=HTMLResponse,
    name="header_pages_run",
)
def header_pages_run(
    request: Request,
    document_id: uuid.UUID,
    start_page: int = Form(DEFAULT_START_PAGE),
    end_page: int = Form(DEFAULT_END_PAGE),
    llm: str = Form(DEFAULT_LLM),
    use_group: bool = Form(False),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Run the Pages method on one document and report the outcome as a row.

    One document per request, by design: the queue on the page calls this once
    per eligible document, so a slow parse holds up one row rather than the
    whole batch. Failures render as a row too — the caller is a status cell,
    and a 4xx would leave it blank.

    ``use_group`` is the Group method: the page range (and the model, when the
    strategy names one) is resolved here from the document's group id rather
    than trusted from the browser.
    """
    doc = _document_or_404(db, document_id)

    result = (
        run_group(db, doc, llm=llm)
        if use_group
        else run_pages(db, doc, start_page, end_page, llm=llm)
    )

    return templates.TemplateResponse(
        request,
        "headers/_partials/pages_row.html",
        {"result": result},
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
    llm: str = Form(DEFAULT_LLM),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Turn the pasted text into headers and store them.

    A failure re-renders the dialog with the text still in it — the paste is
    the expensive part for whoever is doing this, and losing it to an error
    message would be the worst part of the whole screen.
    """
    doc = _document_or_404(db, document_id)

    try:
        extracted = extract_headers(contents, llm=llm)
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


@router.post(
    "/{document_id}/indexes",
    response_class=HTMLResponse,
    name="header_indexes_update",
)
def header_indexes_update(
    request: Request,
    document_id: uuid.UUID,
    indexes: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Save hand-edited start/end page indexes and re-render the outline.

    A bad payload re-renders the stored values with the message in the dialog
    rather than as an HTTP error — the caller is the pop-up's Save button, and
    a 4xx would leave the pop-up blank.
    """
    doc = _document_or_404(db, document_id)

    error: str | None = None
    updated: int | None = None
    try:
        updated = update_page_indexes(db, doc.id, parse_index_edits(indexes))
    except ValueError as exc:
        error = str(exc)

    return templates.TemplateResponse(
        request,
        "headers/_partials/outline_modal.html",
        {
            "doc": doc,
            "roots": document_headers(db, doc.id),
            "saved": None,
            "total": header_count(db, doc.id),
            "updated": updated,
            "error": error,
        },
    )


def _transfer_modal(
    request: Request,
    db: Session,
    doc,
    *,
    outcome=None,
    outcome_target: str | None = None,
    error: str | None = None,
    notice: str | None = None,
    edit_error: str | None = None,
) -> HTMLResponse:
    """The Transfer pop-up: bucket TOC on the left, stored headers on the right.

    The TOC is read fresh on every render — including the ones after an apply
    or an edit, so the panes always show what the bucket and the table hold
    now. A TOC that cannot be read renders as a message in its pane, and a bad
    edit as a bar in the dialog, rather than as an HTTP error: the caller is a
    button on the page, and a 4xx would leave the pop-up blank.
    """
    toc = None
    toc_error = error
    if toc_error is None:
        try:
            toc = load_bucket_toc(doc)
        except TransferError as exc:
            toc_error = str(exc)

    return templates.TemplateResponse(
        request,
        "headers/_partials/transfer_modal.html",
        {
            "doc": doc,
            "roots": document_headers(db, doc.id),
            "total": header_count(db, doc.id),
            "toc": toc,
            "toc_error": toc_error,
            "outcome": outcome,
            "outcome_target": outcome_target,
            "notice": notice,
            "edit_error": edit_error,
            # What the Stored pane paints red: per header id, the title runs
            # and page pills that disagree with the TOC.
            "diffs": diff_for_document(db, doc.id, toc.roots) if toc else {},
        },
    )


@router.get(
    "/{document_id}/transfer",
    response_class=HTMLResponse,
    name="header_transfer_form",
)
def header_transfer_form(
    request: Request, document_id: uuid.UUID, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Open the Transfer pop-up for one document."""
    doc = _document_or_404(db, document_id)

    return _transfer_modal(request, db, doc)


@router.post(
    "/{document_id}/transfer",
    response_class=HTMLResponse,
    name="header_transfer_apply",
)
def header_transfer_apply(
    request: Request, document_id: uuid.UUID, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Write the bucket TOC into the document's headers and re-render the pop-up.

    The TOC is re-read from the bucket here rather than trusted from the
    browser, so the button is safe to click on a stale pop-up.
    """
    doc = _document_or_404(db, document_id)

    outcome = None
    error: str | None = None
    try:
        toc = load_bucket_toc(doc)
        outcome = apply_toc(db, doc.id, toc.roots)
    except TransferError as exc:
        error = str(exc)

    return _transfer_modal(
        request, db, doc, outcome=outcome, outcome_target="stored headers", error=error
    )


@router.post(
    "/{document_id}/transfer/to-bucket",
    response_class=HTMLResponse,
    name="header_transfer_to_bucket",
)
def header_transfer_to_bucket(
    request: Request, document_id: uuid.UUID, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Replace the bucket's TOC with the stored headers and re-render the pop-up.

    The other direction's twin, but a replace, not an upsert: the metadata
    JSON keeps everything except its TOC, which is rebuilt from the table. A
    problem with the file renders as the bar in the dialog rather than as an
    HTTP error.
    """
    doc = _document_or_404(db, document_id)

    notice = None
    edit_error: str | None = None
    try:
        written = apply_headers_to_bucket(db, doc)
        notice = (
            f"bucket TOC replaced with the {written} stored "
            f"header{'' if written == 1 else 's'}"
        )
    except TransferError as exc:
        edit_error = str(exc)

    return _transfer_modal(request, db, doc, notice=notice, edit_error=edit_error)


@router.post(
    "/{document_id}/transfer/toc",
    response_class=HTMLResponse,
    name="header_transfer_toc_update",
)
def header_transfer_toc_update(
    request: Request,
    document_id: uuid.UUID,
    edits: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Save the bucket pane's hand-edits into the metadata JSON and re-render.

    This writes to the bucket only — the stored headers are not touched; the
    Stored pane's editor below is its separate counterpart.
    """
    doc = _document_or_404(db, document_id)

    notice = None
    edit_error: str | None = None
    try:
        changed = update_bucket_toc(doc, parse_row_edits(edits))
        notice = f"{changed} TOC row{'' if changed == 1 else 's'} updated in the bucket"
    except (ValueError, TransferError) as exc:
        edit_error = str(exc)

    return _transfer_modal(request, db, doc, notice=notice, edit_error=edit_error)


@router.post(
    "/{document_id}/transfer/toc/delete",
    response_class=HTMLResponse,
    name="header_transfer_toc_delete",
)
def header_transfer_toc_delete(
    request: Request,
    document_id: uuid.UUID,
    seq: int = Form(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Delete one heading (and its sub-headings) from the bucket's TOC.

    Bucket only — the stored headers are never touched by this. The entry is
    named by its preorder seq, the identity the pop-up's rows carry; a stale
    seq is refused with a message rather than deleting whatever sits there now.
    """
    doc = _document_or_404(db, document_id)

    notice = None
    edit_error: str | None = None
    try:
        deleted = delete_toc_entry(doc, seq)
        notice = (
            f"deleted “{deleted.title}” "
            f"({deleted.removed} row{'' if deleted.removed == 1 else 's'}) "
            "from the bucket TOC"
        )
    except TransferError as exc:
        edit_error = str(exc)

    return _transfer_modal(request, db, doc, notice=notice, edit_error=edit_error)


@router.post(
    "/{document_id}/transfer/stored",
    response_class=HTMLResponse,
    name="header_transfer_stored_update",
)
def header_transfer_stored_update(
    request: Request,
    document_id: uuid.UUID,
    edits: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Save the Stored pane's hand-edits into the headers table and re-render.

    This writes to the database only — the bucket's metadata JSON is not
    touched; the bucket pane's editor above is its separate counterpart.
    """
    doc = _document_or_404(db, document_id)

    notice = None
    edit_error: str | None = None
    try:
        changed = update_stored_headers(db, doc.id, parse_row_edits(edits))
        notice = f"{changed} stored header{'' if changed == 1 else 's'} updated"
    except ValueError as exc:
        edit_error = str(exc)

    return _transfer_modal(request, db, doc, notice=notice, edit_error=edit_error)


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
