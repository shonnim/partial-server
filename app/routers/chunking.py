import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.chunking import (
    CHUNKERS,
    MAX_CHUNK_BATCH,
    chunkable_group_ids,
    document_chunks,
    run_chunking,
)
from app.services.documents import get_document
from app.services.tree import (
    DEFAULT_FILE_PAGE,
    list_level,
    resolve_selection,
    root_totals,
)
from app.templating import templates

router = APIRouter(prefix="/chunking", tags=["chunking"])


@router.get("", response_class=HTMLResponse, name="chunking_page")
def chunking_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """The same corpus tree as Grouping, with a group picker beside it."""
    total, failed = root_totals(db)

    return templates.TemplateResponse(
        request,
        "chunking/index.html",
        {
            "active_nav": "chunking",
            "level": list_level(db),
            "total": total,
            "failed": failed,
            "max_batch": MAX_CHUNK_BATCH,
            "group_ids": chunkable_group_ids(db),
            "implemented": sorted(CHUNKERS),
        },
    )


@router.get("/nodes", response_class=HTMLResponse, name="chunking_nodes")
def chunking_nodes(
    request: Request,
    path: str = "",
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """One level of the tree, fetched when a folder is expanded."""
    level = list_level(db, path, file_offset=offset, file_limit=DEFAULT_FILE_PAGE)
    template = (
        "chunking/_partials/files.html" if offset else "chunking/_partials/level.html"
    )

    return templates.TemplateResponse(request, template, {"level": level})


@router.post("/run", response_class=HTMLResponse, name="chunking_run")
def chunking_run(
    request: Request,
    selection: str = Form(""),
    group_id: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Chunk the selected documents with the chosen group's strategy.

    Errors come back as the result card too: the caller is a button on the
    page, and a 4xx would leave the panel blank.
    """

    def fail(reason: str) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "chunking/_partials/result.html", {"error": reason}
        )

    group_id = group_id.strip()
    if not group_id:
        return fail("Pick a group id first.")

    doc_ids, error = resolve_selection(db, selection, limit=MAX_CHUNK_BATCH)
    if error:
        return fail(error)

    result = run_chunking(db, group_id, doc_ids)
    if result.error:
        return fail(result.error)

    return templates.TemplateResponse(
        request, "chunking/_partials/result.html", {"result": result}
    )


@router.get(
    "/{document_id}/chunks", response_class=HTMLResponse, name="chunking_chunks"
)
def chunking_chunks(
    request: Request, document_id: uuid.UUID, db: Session = Depends(get_db)
) -> HTMLResponse:
    """The chunks stored for one document, as the pop-up the count opens."""
    doc = get_document(db, document_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")

    return templates.TemplateResponse(
        request,
        "chunking/_partials/chunks_modal.html",
        {"doc": doc, "chunks": document_chunks(db, doc.id)},
    )
