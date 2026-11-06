from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.services.transform import MAX_TRANSFORM_BATCH, migrate_documents
from app.services.tree import (
    DEFAULT_FILE_PAGE,
    list_level,
    resolve_selection,
    root_totals,
)
from app.templating import templates

router = APIRouter(prefix="/transform", tags=["transform"])


@router.get("", response_class=HTMLResponse, name="transform_page")
def transform_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """The transform tree. Only the root level is resolved up front."""
    total, failed = root_totals(db)

    return templates.TemplateResponse(
        request,
        "transform/index.html",
        {
            "active_nav": "transform",
            "level": list_level(db),
            "total": total,
            "failed": failed,
            "max_batch": MAX_TRANSFORM_BATCH,
            "rag_bucket": get_settings().bucket_rag_files,
        },
    )


@router.get("/nodes", response_class=HTMLResponse, name="transform_nodes")
def transform_nodes(
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
        "transform/_partials/files.html" if offset else "transform/_partials/level.html"
    )

    return templates.TemplateResponse(request, template, {"level": level})


@router.post("/migrate", response_class=HTMLResponse, name="transform_migrate")
def transform_migrate(
    request: Request,
    selection: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Migrate every selected document into the RAG files bucket.

    Errors render as the result box too — the caller is a button on the page,
    and a 4xx would leave the panel blank.
    """
    doc_ids, error = resolve_selection(db, selection, limit=MAX_TRANSFORM_BATCH)
    if error:
        return templates.TemplateResponse(
            request, "transform/_partials/result.html", {"error": error}
        )

    result = migrate_documents(db, doc_ids)

    return templates.TemplateResponse(
        request, "transform/_partials/result.html", {"result": result}
    )
