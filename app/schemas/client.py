"""Response bodies of the client API the Next.js app consumes."""

import uuid

from pydantic import BaseModel


class ClientDocumentOut(BaseModel):
    doc_id: uuid.UUID
    filename: str
    # A parse result, so null until PARSING has completed for this document.
    pages: int | None
    # A presigned RustFS URL the client fetches the PDF from directly. It
    # expires (PRESIGN_EXPIRES_SECONDS), so it is for viewing now, not for
    # storing; ask again when it goes stale.
    file_url: str


class ClientHeaderSummaryOut(BaseModel):
    id: int
    header: str
    parent_id: int | None
    start_page: int | None
    start_page_idx: int | None
    level: int
    # The Summarizing run's Korean conclusion for this section; null until
    # that run has reached it.
    conclusion: str | None


class ClientDocSummaryOut(BaseModel):
    # documents.summary — null until the Summarizing run has written it.
    doc_summary: str | None
    # The document's headers in reading order (id asc), each with its conclusion.
    header_summary: list[ClientHeaderSummaryOut]
