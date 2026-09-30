from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.grouping import (
    MAX_GROUP_BATCH,
    MAX_GROUP_ID_LENGTH,
    assign_group,
    group_ids_in_use,
    normalise_group_id,
)
from app.services.tree import (
    DEFAULT_FILE_PAGE,
    list_level,
    resolve_selection,
    root_totals,
)
from app.templating import templates

router = APIRouter(prefix="/grouping", tags=["grouping"])


@router.get("", response_class=HTMLResponse, name="grouping_page")
def grouping_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """The grouping tree. Only the root level is resolved up front."""
    total, failed = root_totals(db)

    return templates.TemplateResponse(
        request,
        "grouping/index.html",
        {
            "active_nav": "grouping",
            "level": list_level(db),
            "total": total,
            "failed": failed,
            "max_batch": MAX_GROUP_BATCH,
            "max_length": MAX_GROUP_ID_LENGTH,
            "known_groups": group_ids_in_use(db),
        },
    )


@router.get("/nodes", response_class=HTMLResponse, name="grouping_nodes")
def grouping_nodes(
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
        "grouping/_partials/files.html" if offset else "grouping/_partials/level.html"
    )

    return templates.TemplateResponse(request, template, {"level": level})


@router.post("/assign", response_class=HTMLResponse, name="grouping_assign")
def grouping_assign(
    request: Request,
    selection: str = Form(""),
    group_id: str = Form(""),
    clear: bool = Form(False),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Stamp the typed group id on every selected document.

    ``clear`` sets the group back to NULL instead, and then the typed label is
    ignored. Errors render as the result box too — the caller is a button on
    the page, and a 4xx would leave the panel blank.
    """

    def fail(reason: str) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "grouping/_partials/result.html", {"error": reason}
        )

    label = None if clear else normalise_group_id(group_id)
    if not clear and label is None:
        return fail("Enter a group id first.")
    if label is not None and len(label) > MAX_GROUP_ID_LENGTH:
        return fail(
            f"Group id is too long. Keep it under {MAX_GROUP_ID_LENGTH} characters."
        )

    doc_ids, error = resolve_selection(db, selection, limit=MAX_GROUP_BATCH)
    if error:
        return fail(error)

    result = assign_group(db, doc_ids, label)

    return templates.TemplateResponse(
        request, "grouping/_partials/result.html", {"result": result}
    )
