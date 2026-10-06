"""The JSON API the Next.js client app talks to.

Unlike the HTMX pages, every route here returns JSON and reports errors as
status codes, same as the retrieval API.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.client import ClientDocumentOut
from app.services.documents import get_document
from app.storage import presign_get

router = APIRouter(prefix="/document", tags=["client"])


@router.get("/{doc_id}", response_model=ClientDocumentOut, name="client_document")
def client_document(doc_id: uuid.UUID, db: Session = Depends(get_db)) -> ClientDocumentOut:
    """One document's metadata plus a presigned RustFS link to its PDF."""
    doc = get_document(db, doc_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")

    return ClientDocumentOut(
        doc_id=doc.id,
        filename=doc.filename,
        pages=doc.pages,
        file_url=presign_get(doc.bucket, doc.s3_key, filename=doc.filename),
    )
