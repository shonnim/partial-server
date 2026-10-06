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
