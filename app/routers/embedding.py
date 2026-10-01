import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.chunk import Chunk
from app.services.documents import get_document
from app.services.embedding import (
    STATUSES,
    PanelChunk,
    build_queue,
    chunk_detail,
    collection_overview,
    decorate_level,
    delete_document_vectors,
    document_panel,
    effective_status,
    embed_chunk,
    embed_document,
    folder_panel,
)
from app.services.tree import (
    DEFAULT_FILE_PAGE,
    list_level,
    resolve_selection,
    root_totals,
)
from app.templating import templates
from app.vectors.client import VectorStoreError

router = APIRouter(prefix="/embedding", tags=["embedding"])

# Documents one Embed click may queue, walked one per request by the browser.
MAX_EMBED_BATCH = 200


def _document_or_404(db: Session, document_id: uuid.UUID):
    doc = get_document(db, document_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    return doc


def _chunk_or_404(db: Session, chunk_id: int) -> Chunk:
    chunk = db.get(Chunk, chunk_id)
    if chunk is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "chunk not found")
    return chunk


@router.get("", response_class=HTMLResponse, name="embedding_page")
def embedding_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """The same corpus tree, read for embedding state instead."""
    total, failed = root_totals(db)

    return templates.TemplateResponse(
        request,
        "embedding/index.html",
        {
            "active_nav": "embedding",
            "total": total,
            "failed": failed,
            "max_batch": MAX_EMBED_BATCH,
            **decorate_level(db, list_level(db)),
        },
    )


@router.get("/nodes", response_class=HTMLResponse, name="embedding_nodes")
def embedding_nodes(
    request: Request,
    path: str = "",
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """One level of the tree, fetched when a folder is expanded."""
    level = list_level(db, path, file_offset=offset, file_limit=DEFAULT_FILE_PAGE)
    template = (
        "embedding/_partials/files.html" if offset else "embedding/_partials/level.html"
    )

    return templates.TemplateResponse(request, template, decorate_level(db, level))


@router.get("/collection", response_class=HTMLResponse, name="embedding_collection")
def embedding_collection(request: Request) -> HTMLResponse:
    """The collection info box: what Qdrant holds, and whether it answers."""
    return templates.TemplateResponse(
        request,
        "embedding/_partials/collection.html",
        {"overview": collection_overview()},
    )


@router.post("/run", response_class=HTMLResponse, name="embedding_run")
def embedding_run(
    request: Request,
    selection: str = Form(""),
    force: bool = Form(False),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Turn the selection into a queue the browser walks one document at a time.

    Errors come back as a rendered fragment rather than an HTTP error: the
    caller is a button on the page, and a 4xx would leave the panel blank.
    """
    doc_ids, error = resolve_selection(db, selection, limit=MAX_EMBED_BATCH)
    if error:
        return templates.TemplateResponse(
            request, "embedding/_partials/queue.html", {"error": error}
        )

    return templates.TemplateResponse(
        request,
        "embedding/_partials/queue.html",
        {"queue": build_queue(db, doc_ids, force=force), "force": force},
    )


@router.post(
    "/{document_id}/run", response_class=HTMLResponse, name="embedding_run_one"
)
def embedding_run_one(
    request: Request,
    document_id: uuid.UUID,
    force: bool = Form(False),
    render: str = Form("row"),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Embed one document's chunks.

    One document per request, by design: every chunk is an API call, and the
    browser driving the queue is what keeps a batch from being one long HTTP
    request. The queue reads the reply as a row; the buttons on the document
    panel ask for the refreshed panel instead with ``render=panel``.
    """
    doc = _document_or_404(db, document_id)
    result = embed_document(db, doc, force=force)

    if render == "panel":
        return templates.TemplateResponse(
            request,
            "embedding/_partials/doc_panel.html",
            {
                "panel": document_panel(db, doc),
                "panel_error": result.error if not result.ok else None,
            },
        )
    return templates.TemplateResponse(
        request, "embedding/_partials/row_status.html", {"result": result}
    )


@router.get(
    "/{document_id}/panel", response_class=HTMLResponse, name="embedding_panel"
)
def embedding_panel(
    request: Request,
    document_id: uuid.UUID,
    status_filter: str = Query("", alias="status"),
    q: str = "",
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """The center panel for one document: its chunks and their statuses."""
    doc = _document_or_404(db, document_id)
    if status_filter not in ("", *STATUSES):
        status_filter = ""

    return templates.TemplateResponse(
        request,
        "embedding/_partials/doc_panel.html",
        {"panel": document_panel(db, doc, status_filter=status_filter, query=q)},
    )


@router.get("/folder", response_class=HTMLResponse, name="embedding_folder")
def embedding_folder(
    request: Request, path: str = "", db: Session = Depends(get_db)
) -> HTMLResponse:
    """The center panel for one folder: its documents and their standing."""
    return templates.TemplateResponse(
        request,
        "embedding/_partials/folder_panel.html",
        {"panel": folder_panel(db, path)},
    )


@router.post(
    "/{document_id}/delete-vectors",
    response_class=HTMLResponse,
    name="embedding_delete_vectors",
)
def embedding_delete_vectors(
    request: Request, document_id: uuid.UUID, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Drop a document's vectors and reset its rows, then re-render the panel."""
    doc = _document_or_404(db, document_id)
    error = None
    try:
        delete_document_vectors(db, doc)
    except VectorStoreError as exc:
        error = str(exc)

    return templates.TemplateResponse(
        request,
        "embedding/_partials/doc_panel.html",
        {"panel": document_panel(db, doc), "panel_error": error},
    )


@router.post(
    "/chunk/{chunk_id}/embed", response_class=HTMLResponse, name="embedding_chunk_embed"
)
def embedding_chunk_embed(
    request: Request, chunk_id: int, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Embed one chunk and send back its refreshed table row."""
    chunk = _chunk_or_404(db, chunk_id)
    embed_chunk(db, chunk)

    return templates.TemplateResponse(
        request,
        "embedding/_partials/chunk_row.html",
        {"item": PanelChunk(chunk=chunk, status=effective_status(chunk))},
    )


@router.get(
    "/chunk/{chunk_id}", response_class=HTMLResponse, name="embedding_chunk_detail"
)
def embedding_chunk_detail(
    request: Request, chunk_id: int, db: Session = Depends(get_db)
) -> HTMLResponse:
    """The sidebar detail for one chunk, Qdrant point included."""
    chunk = _chunk_or_404(db, chunk_id)

    return templates.TemplateResponse(
        request,
        "embedding/_partials/chunk_detail.html",
        {"detail": chunk_detail(chunk)},
    )
