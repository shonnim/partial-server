"""The JSON API the Next.js client app talks to.

Unlike the HTMX pages, every route here returns JSON and reports errors as
status codes, same as the retrieval API.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.client import (
    ClientDocSummaryOut,
    ClientDocumentOut,
    ClientHeaderSummaryOut,
)
from app.services.documents import get_document
from app.services.sections import document_headers_in_order
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


@router.get(
    "/{doc_id}/summary",
    response_model=ClientDocSummaryOut,
    name="client_document_summary",
)
def client_document_summary(
    doc_id: uuid.UUID, db: Session = Depends(get_db)
) -> ClientDocSummaryOut:
    """One document's general summary plus its headers with their conclusions."""
    doc = get_document(db, doc_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")

    return ClientDocSummaryOut(
        doc_summary=doc.summary,
        header_summary=[
            ClientHeaderSummaryOut(
                id=header.id,
                header=header.header,
                parent_id=header.parent_id,
                start_page=header.start_page,
                start_page_idx=header.start_page_idx,
                level=header.level,
                conclusion=header.conclusion,
            )
            for header in document_headers_in_order(db, doc_id)
        ],
    )
