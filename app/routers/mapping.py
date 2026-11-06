import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.mapping import MappingEntry
from app.services.documents import get_document
from app.services.mapping import (
    CheckResult,
    check_mapping,
    normalize_name,
    save_entry,
    save_mapping,
)
from app.services.tree import DEFAULT_FILE_PAGE, list_level, root_totals
from app.templating import templates

router = APIRouter(prefix="/mapping", tags=["mapping"])

_ENTRIES = TypeAdapter(list[MappingEntry])
_ENTRY = TypeAdapter(MappingEntry)


@router.get("", response_class=HTMLResponse, name="mapping_page")
def mapping_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """The mapping tree. Only the root level is resolved up front."""
    total, failed = root_totals(db)

    return templates.TemplateResponse(
        request,
        "mapping/index.html",
        {
            "active_nav": "mapping",
            "level": list_level(db),
            "total": total,
            "failed": failed,
        },
    )


@router.get("/nodes", response_class=HTMLResponse, name="mapping_nodes")
def mapping_nodes(
    request: Request,
    path: str = "",
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """One level of the tree, fetched when a folder is expanded."""
    level = list_level(db, path, file_offset=offset, file_limit=DEFAULT_FILE_PAGE)
    template = (
        "mapping/_partials/files.html" if offset else "mapping/_partials/level.html"
    )

    return templates.TemplateResponse(request, template, {"level": level})


@router.post("/check", response_class=HTMLResponse, name="mapping_check")
def mapping_check(
    request: Request,
    path: str = Form(""),
    payload: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Compare the pasted JSON against the documents under ``path``.

    Errors render as the result box too — the caller is a button on the page,
    and a 4xx would leave the panel blank.
    """
    entries, parse_error = _parse_payload(payload)
    if parse_error:
        return _result(request, error=parse_error)

    result, error = check_mapping(db, path, entries)
    return _result(request, result=result, error=error)


@router.post("/save", response_class=HTMLResponse, name="mapping_save")
def mapping_save(
    request: Request,
    path: str = Form(""),
    payload: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Re-check the pasted JSON, then write the clean matches to the documents."""
    entries, parse_error = _parse_payload(payload)
    if parse_error:
        return _result(request, error=parse_error)

    save, error = save_mapping(db, path, entries)
    if error:
        return _result(request, error=error)

    return _result(request, result=save.check, saved=save.saved)


@router.get(
    "/{document_id}/manual", response_class=HTMLResponse, name="mapping_manual_form"
)
def mapping_manual_form(
    request: Request, document_id: uuid.UUID, db: Session = Depends(get_db)
) -> HTMLResponse:
    """The paste-one-JSON-block dialog for one document."""
    doc = _document_or_404(db, document_id)

    return templates.TemplateResponse(
        request,
        "mapping/_partials/manual_modal.html",
        {"doc": doc, "payload": ""},
    )


@router.post(
    "/{document_id}/manual", response_class=HTMLResponse, name="mapping_manual_submit"
)
def mapping_manual_submit(
    request: Request,
    document_id: uuid.UUID,
    payload: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Write the pasted block's ids onto this document, no name matching.

    A failure re-renders the dialog with the paste still in it. A 파일명 that
    does not normalise to this file's name is saved anyway — manual exists to
    override the matcher — but the result says so out loud.
    """
    doc = _document_or_404(db, document_id)

    entry, error = _parse_single(payload)
    if error:
        return templates.TemplateResponse(
            request,
            "mapping/_partials/manual_modal.html",
            {"doc": doc, "payload": payload, "error": error},
        )

    save_entry(db, doc.id, entry)
    db.refresh(doc)

    return templates.TemplateResponse(
        request,
        "mapping/_partials/manual_modal.html",
        {
            "doc": doc,
            "payload": payload,
            "saved": True,
            "name_differs": normalize_name(entry.name) != normalize_name(doc.filename),
            "entry_name": entry.name,
        },
    )


def _document_or_404(db: Session, document_id: uuid.UUID):
    doc = get_document(db, document_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    return doc


def _parse_single(payload: str) -> tuple[MappingEntry | None, str | None]:
    """One pasted block: a bare object, or a list holding exactly one."""
    if not payload.strip():
        return None, "Paste the JSON block first."
    try:
        if payload.lstrip().startswith("["):
            entries = _ENTRIES.validate_json(payload)
            if len(entries) != 1:
                return None, (
                    f"The list holds {len(entries)} entries — paste exactly the "
                    "one block that belongs to this file."
                )
            return entries[0], None
        return _ENTRY.validate_json(payload), None
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "input"
        return None, f"The JSON could not be read: {where} — {first['msg']}."


def _parse_payload(payload: str) -> tuple[list[MappingEntry], str | None]:
    if not payload.strip():
        return [], "Paste the JSON first."
    try:
        return _ENTRIES.validate_json(payload), None
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "input"
        return [], f"The JSON could not be read: {where} — {first['msg']}."


def _result(
    request: Request,
    *,
    result: CheckResult | None = None,
    error: str | None = None,
    saved: int | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "mapping/_partials/check_result.html",
        {"result": result, "error": error, "saved": saved},
    )
