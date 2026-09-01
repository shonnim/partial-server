from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.tree import DEFAULT_FILE_PAGE, list_level, root_totals
from app.templating import templates

router = APIRouter(prefix="/parsing", tags=["parsing"])


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
