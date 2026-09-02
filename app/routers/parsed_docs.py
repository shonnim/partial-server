from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.parsed_doc import ParsedDocBulkIn, ParsedDocBulkOut
from app.services.parsed_docs import (
    DuplicateIdsInBatch,
    UnknownDocumentIds,
    upsert_parsed_docs,
)

router = APIRouter(prefix="/parsed-docs", tags=["parsed-docs"])


@router.post("", response_model=ParsedDocBulkOut, status_code=status.HTTP_201_CREATED)
def create_parsed_docs(
    payload: ParsedDocBulkIn, db: Session = Depends(get_db)
) -> ParsedDocBulkOut:
    """Add a batch of parsed-doc rows, refreshing any id that already exists."""
    try:
        inserted = upsert_parsed_docs(db, payload.data)
    except (DuplicateIdsInBatch, UnknownDocumentIds) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"message": str(exc), "ids": exc.ids},
        ) from exc

    return ParsedDocBulkOut(inserted=inserted)
