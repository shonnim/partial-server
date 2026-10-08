"""The question-answering API's request body and stream payloads."""

from pydantic import BaseModel, Field

# The three steps the client shows while an answer is made, with the labels
# it prints — the backend owns the wording so every client says the same.
STEP_LABELS = {
    "question-checking": "질문 의도를 분석하고 있습니다...",
    "context-finding": "보고서 본문에서 근거를 찾고 있습니다...",
    "generating": "답변을 작성하고 있습니다...",
}


class QAAskRequest(BaseModel):
    # The document the answer is drawn from rides in the path.
    query: str = Field(min_length=1, description="The user's question or request.")


class QASourceOut(BaseModel):
    """One block the answer was drawn from, as the result event carries it."""

    # The parse page the block sits on (0-based), and the page number printed
    # on that page as the content list read it — None when it shows none.
    page_idx: int | None
    page: int | None
    text: str
    # The block's bbox, first to fourth value.
    bbox_1: int | None
    bbox_2: int | None
    bbox_3: int | None
    bbox_4: int | None


class QAResultOut(BaseModel):
    """The ``result`` event: the answer and where it came from."""

    answer: str
    sources: list[QASourceOut]
