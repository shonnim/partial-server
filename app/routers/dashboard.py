from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.document import Document
from app.templating import templates

router = APIRouter(tags=["ui"])


@router.get("/", response_class=HTMLResponse, name="dashboard_page")
def dashboard_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Landing page. Two counts today; the real panels are not built yet."""
    total_documents = db.scalar(select(func.count()).select_from(Document)) or 0
    total_categories = (
        db.scalar(select(func.count(func.distinct(Document.category)))) or 0
    )

    return templates.TemplateResponse(
        request,
        "dashboard/index.html",
        {
            "active_nav": "dashboard",
            "total_documents": total_documents,
            "total_categories": total_categories,
        },
    )
