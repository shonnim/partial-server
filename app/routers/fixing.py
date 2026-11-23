from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.fixing import (
    MAX_FIX_BATCH,
    check_documents,
    check_pages,
    fix_documents,
)
from app.services.tree import (
    DEFAULT_FILE_PAGE,
    list_level,
    resolve_selection,
    root_totals,
)
from app.templating import templates

router = APIRouter(prefix="/fixing", tags=["fixing"])


@router.get("", response_class=HTMLResponse, name="fixing_page")
def fixing_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """The fixing tree. Only the root level is resolved up front."""
    total, failed = root_totals(db)

    return templates.TemplateResponse(
        request,
        "fixing/index.html",
        {
            "active_nav": "fixing",
            "level": list_level(db),
            "total": total,
            "failed": failed,
            "max_batch": MAX_FIX_BATCH,
        },
    )


@router.get("/nodes", response_class=HTMLResponse, name="fixing_nodes")
def fixing_nodes(
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
        "fixing/_partials/files.html" if offset else "fixing/_partials/level.html"
    )

    return templates.TemplateResponse(request, template, {"level": level})


@router.post("/check", response_class=HTMLResponse, name="fixing_check")
def fixing_check(
    request: Request,
    selection: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Report which selected PDFs hold two-up sheets, writing nothing.

    Errors render as the result box too — the caller is a button on the page,
    and a 4xx would leave the panel blank.
    """
    doc_ids, error = resolve_selection(db, selection, limit=MAX_FIX_BATCH)
    if error:
        return templates.TemplateResponse(
            request, "fixing/_partials/result.html", {"error": error}
        )

    result = check_documents(db, doc_ids)

    return templates.TemplateResponse(
        request, "fixing/_partials/result.html", {"mode": "check", "check": result}
    )


@router.post("/pages", response_class=HTMLResponse, name="fixing_pages")
def fixing_pages(
    request: Request,
    selection: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Count each selected PDF's real pages and correct rows that disagree."""
    doc_ids, error = resolve_selection(db, selection, limit=MAX_FIX_BATCH)
    if error:
        return templates.TemplateResponse(
            request, "fixing/_partials/result.html", {"error": error}
        )

    result = check_pages(db, doc_ids)

    return templates.TemplateResponse(
        request, "fixing/_partials/result.html", {"mode": "pages", "pages": result}
    )


@router.post("/fix", response_class=HTMLResponse, name="fixing_fix")
def fixing_fix(
    request: Request,
    selection: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Rewrite every selected two-up PDF into one page per sheet."""
    doc_ids, error = resolve_selection(db, selection, limit=MAX_FIX_BATCH)
    if error:
        return templates.TemplateResponse(
            request, "fixing/_partials/result.html", {"error": error}
        )

    result = fix_documents(db, doc_ids)

    return templates.TemplateResponse(
        request, "fixing/_partials/result.html", {"mode": "fix", "fix": result}
    )
