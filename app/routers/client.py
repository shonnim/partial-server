"""The JSON API the client app talks to.

Unlike the HTMX pages, every route here returns JSON and reports errors as
status codes, same as the retrieval API. The one exception is the Q&A
endpoint, which answers over Server-Sent Events:

    POST /document/{doc_id}/ask

    event: progress   data: {"step", "label", "status"}
    event: result     data: {"answer", "sources": [...]}
    event: error      data: {"message"}

Each step is announced ``running`` and then ``completed``; the stream ends
with one ``result`` or one ``error``. The pipeline runs inside the request on
purpose — one question, one document, a handful of model calls, no batch.

Cancellation is the client closing the connection (aborting its fetch): the
server's stream task is cancelled with it, so no further step starts; a model
call already in flight finishes in its worker thread and its reply is
discarded.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.db import SessionLocal, get_db
from app.embedding.client import EmbeddingError
from app.llm import LLMError
from app.schemas.client import (
    ClientDocSummaryOut,
    ClientDocumentOut,
    ClientHeaderSummaryOut,
)
from app.schemas.qa import STEP_LABELS, QAAskRequest, QAResultOut, QASourceOut
from app.services.documents import get_document
from app.services.qa import (
    NOT_FOUND_MESSAGE,
    chunk_answers,
    classify_query,
    find_stored_summary,
    generate_answer,
    generate_summary_answer,
    identify_sources,
    query_pages_exist,
    requested_pages,
    retrieve_chunk,
    suggest_questions,
)
from app.services.sections import document_headers_in_order
from app.storage import presign_get
from app.vectors.client import VectorStoreError

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


# ---- Q&A --------------------------------------------------------------------------


def _event(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _progress(step: str, state: str) -> str:
    return _event(
        "progress", {"step": step, "label": STEP_LABELS[step], "status": state}
    )


def _result(answer: str, sources: list[QASourceOut], suggestions: list[str]) -> str:
    return _event(
        "result",
        QAResultOut(
            answer=answer, sources=sources, suggested_questions=suggestions
        ).model_dump(),
    )


async def _answer_stream(query: str, doc_id: uuid.UUID) -> AsyncIterator[str]:
    """The Q&A pipeline, one SSE string per event.

    Blocking work — model calls, the embedding, the queries — runs in worker
    threads so the event loop keeps serving between steps, and so a client
    disconnect cancels this generator at the next await. The stream holds its
    own session: the request dependency's may close before streaming ends.
    """
    try:
        yield _progress("question-checking", "running")
        # A query that points at a page the document does not reach is refused
        # here, before any model call. Only a query naming a page needs the
        # document's content list, so only that one opens a session.
        if requested_pages(query):
            with SessionLocal() as db:
                document = get_document(db, doc_id)
                pages_ok = (
                    await asyncio.to_thread(query_pages_exist, db, document, query)
                    if document is not None
                    else True
                )
            if not pages_ok:
                yield _progress("question-checking", "completed")
                yield _result(NOT_FOUND_MESSAGE, [], [])
                return

        classification = await asyncio.to_thread(classify_query, query)
        yield _progress("question-checking", "completed")
        if classification.query_type == "unclear":
            yield _result(NOT_FOUND_MESSAGE, [], [])
            return

        yield _progress("context-finding", "running")

        # A summary request is routed by scope before any retrieval: the
        # Summarizing run's stored summary — documents.summary for the whole
        # report, the header's conclusion for one section — already answers
        # it. Only a summary request with no stored summary to serve falls
        # through to the body pipeline below.
        if classification.query_type == "summary":
            with SessionLocal() as db:
                document = get_document(db, doc_id)
                stored = (
                    await asyncio.to_thread(find_stored_summary, db, document, query)
                    if document is not None
                    else None
                )
                if stored is not None:
                    yield _progress("context-finding", "completed")
                    yield _progress("generating", "running")
                    answer, suggestions = await asyncio.gather(
                        asyncio.to_thread(
                            generate_summary_answer,
                            query,
                            stored.text,
                            section=stored.section,
                        ),
                        asyncio.to_thread(suggest_questions, db, doc_id),
                    )
                    yield _progress("generating", "completed")
                    # A stored summary spans pages, so it names no source block.
                    yield _result(answer, [], suggestions)
                    return

        with SessionLocal() as db:
            document = get_document(db, doc_id)
            chunk = (
                await asyncio.to_thread(
                    retrieve_chunk, db, doc_id=doc_id, query=classification.search_query
                )
                if document is not None
                else None
            )
            # The nearest chunk is not necessarily the answering chunk: one
            # model call verifies the passage actually states what the query
            # needs before an answer is written over it.
            grounded = (
                await asyncio.to_thread(chunk_answers, query, chunk.text or chunk.emb_text)
                if chunk is not None
                else False
            )
            yield _progress("context-finding", "completed")
            if document is None or chunk is None or not grounded:
                # Nothing of this document is embedded (or it vanished since
                # the route checked), or what was found does not hold the
                # answer: there is no body to ground an answer in.
                yield _result(NOT_FOUND_MESSAGE, [], [])
                return

            yield _progress("generating", "running")
            # The 추천질문 box rides along with every answer. Its model call
            # runs beside the answer's, costing no extra wall time; only the
            # suggestion thread touches the session, so the two never share it.
            answer, suggestions = await asyncio.gather(
                asyncio.to_thread(
                    generate_answer,
                    query,
                    chunk.text or chunk.emb_text,
                    classification.query_type,
                ),
                asyncio.to_thread(suggest_questions, db, doc_id),
            )
            sources = await asyncio.to_thread(
                identify_sources, db, document, answer=answer, chunk=chunk
            )
            yield _progress("generating", "completed")

        yield _result(
            answer,
            [
                QASourceOut(
                    page_idx=s.page_idx,
                    page=s.page,
                    text=s.text,
                    bbox_1=s.bbox[0],
                    bbox_2=s.bbox[1],
                    bbox_3=s.bbox[2],
                    bbox_4=s.bbox[3],
                )
                for s in sources
            ],
            suggestions,
        )
    except (EmbeddingError, LLMError, VectorStoreError) as exc:
        yield _event("error", {"message": str(exc)})


@router.post("/{doc_id}/ask", name="client_document_ask")
def client_document_ask(
    doc_id: uuid.UUID, body: QAAskRequest, db: Session = Depends(get_db)
) -> StreamingResponse:
    """One query about one document, answered over an SSE stream.

    What can be refused before any work starts is refused as a status code,
    the way the other routes here do it; everything after the stream begins
    is reported as events.
    """
    if not body.query.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "query is empty")
    if get_document(db, doc_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")

    return StreamingResponse(
        _answer_stream(body.query, doc_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )
