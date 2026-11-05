import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.documents import get_document
from app.services.summarizing import (
    DEFAULT_LLM,
    SUMMARY_LLMS,
    build_general_queue,
    build_queue,
    decorate_level,
    document_summaries,
    make_summarizer,
    resolve_llm,
    summarize_document,
    summarize_document_general,
    summarize_one_header,
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
            "llms": SUMMARY_LLMS,
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
    llm: str = Form(DEFAULT_LLM),
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

    llm = resolve_llm(llm)
    return templates.TemplateResponse(
        request,
        "summarizing/_partials/queue.html",
        {
            "queue": build_queue(db, doc_ids, force=force),
            "force": force,
            "llm": llm,
            "llm_label": SUMMARY_LLMS[llm],
        },
    )


@router.post("/general", response_class=HTMLResponse, name="summarizing_general")
def summarizing_general(
    request: Request,
    selection: str = Form(""),
    force: bool = Form(False),
    llm: str = Form(DEFAULT_LLM),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """The "General Summarize" queue: only the document summary is written.

    A selected document whose header summaries are missing is skipped with a
    pointer at the full Summarize run, which writes them and the general
    summary in one go.
    """
    doc_ids, error = resolve_selection(db, selection, limit=MAX_SUMMARIZE_BATCH)
    if error:
        return templates.TemplateResponse(
            request, "summarizing/_partials/queue.html", {"error": error}
        )

    llm = resolve_llm(llm)
    return templates.TemplateResponse(
        request,
        "summarizing/_partials/queue.html",
        {
            "queue": build_general_queue(db, doc_ids, force=force),
            "force": force,
            "general": True,
            "llm": llm,
            "llm_label": SUMMARY_LLMS[llm],
        },
    )


@router.post(
    "/{document_id}/run", response_class=HTMLResponse, name="summarizing_run_one"
)
def summarizing_run_one(
    request: Request,
    document_id: uuid.UUID,
    force: bool = Form(False),
    llm: str = Form(DEFAULT_LLM),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Summarize one document's headers.

    One document per request, by design: every header is a model call, and
    the browser driving the queue is what keeps a batch from being one long
    HTTP request.
    """
    doc = _document_or_404(db, document_id)
    result = summarize_document(db, doc, force=force, summarizer=make_summarizer(llm))

    return templates.TemplateResponse(
        request, "summarizing/_partials/row_status.html", {"result": result}
    )


@router.post(
    "/{document_id}/general",
    response_class=HTMLResponse,
    name="summarizing_general_one",
)
def summarizing_general_one(
    request: Request,
    document_id: uuid.UUID,
    force: bool = Form(False),
    llm: str = Form(DEFAULT_LLM),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Write one document's general summary from its stored header summaries."""
    doc = _document_or_404(db, document_id)
    result = summarize_document_general(
        db, doc, force=force, summarizer=make_summarizer(llm)
    )

    return templates.TemplateResponse(
        request, "summarizing/_partials/row_status.html", {"result": result}
    )


@router.post(
    "/{document_id}/headers/{header_id}/summarize",
    response_class=HTMLResponse,
    name="summarizing_header_box",
)
def summarizing_header_box(
    request: Request,
    document_id: uuid.UUID,
    header_id: int,
    llm: str = Form(DEFAULT_LLM),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Summarize one header — the button on its box in the pop-up.

    The reply is the rewritten box; the pop-up's pill and the tree row's
    badge ride along out of band so both stay honest without a reload.
    """
    doc = _document_or_404(db, document_id)
    run = summarize_one_header(db, doc, header_id, summarizer=make_summarizer(llm))
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "header not found")

    header, result = run
    return templates.TemplateResponse(
        request,
        "summarizing/_partials/header_box.html",
        {"doc": doc, "header": header, "result": result, "oob": True},
    )


@router.post(
    "/{document_id}/doc-summary", response_class=HTMLResponse, name="summarizing_doc_box"
)
def summarizing_doc_box(
    request: Request,
    document_id: uuid.UUID,
    llm: str = Form(DEFAULT_LLM),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Write the general summary — the button on the pop-up's document box.

    Forced, like the header boxes, and strict: built from the level-1
    summaries only, the source the box promises.
    """
    doc = _document_or_404(db, document_id)
    result = summarize_document_general(
        db, doc, force=True, strict=True, summarizer=make_summarizer(llm)
    )

    return templates.TemplateResponse(
        request,
        "summarizing/_partials/doc_box.html",
        {"doc": doc, "result": result, "oob": True},
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
