"""A minimal client for the Upstage embeddings endpoint.

One call is wrapped: POST one text, get one vector back. Upstage trains the
two sides of retrieval separately, so a chunk goes through the passage model
and a user's question through the query model — ``embed_passage`` and
``embed_query``, same request, different model name. The chunker keeps
chunks well under the API's 4,000-token ceiling, so one text is one request
and there is no batching to manage.

A transport failure, a 429, or a 5xx is retried a few times with exponential
backoff — those are the failures a moment's patience fixes. Any other 4xx is
the request's own fault (an empty input, a text past the token ceiling) and
is raised at once as EmbeddingError with a message worth showing a human.

The API key appears in the Authorization header and nowhere else: not in log
lines, not in error messages, not in anything a template renders. Error
messages carry at most a slice of the response body, never the input text.
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings

# How often a retryable failure is tried again, and the pause before each
# retry doubles: 1s, 2s, 4s.
MAX_RETRIES = 3
BACKOFF_SECONDS = 1.0
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class EmbeddingError(RuntimeError):
    """The embedding could not be made, with the reason."""


@dataclass(frozen=True)
class EmbeddingResult:
    """One passage embedded: the vector and what making it cost."""

    vector: list[float]
    model: str
    prompt_tokens: int

    @property
    def dimension(self) -> int:
        return len(self.vector)


def _read_result(response: httpx.Response) -> EmbeddingResult:
    """The vector out of a 200 reply, or EmbeddingError for a malformed one."""
    try:
        body = response.json()
    except json.JSONDecodeError as exc:
        raise EmbeddingError(f"the embedding reply was not JSON: {exc.msg}") from exc

    data = body.get("data") if isinstance(body, dict) else None
    first = data[0] if isinstance(data, list) and data else None
    vector = first.get("embedding") if isinstance(first, dict) else None
    if (
        not isinstance(vector, list)
        or not vector
        or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vector)
    ):
        raise EmbeddingError("the embedding reply held no usable vector in data[0]")

    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    tokens = usage.get("prompt_tokens")
    return EmbeddingResult(
        vector=[float(v) for v in vector],
        model=str(body.get("model") or ""),
        prompt_tokens=int(tokens) if isinstance(tokens, int) else 0,
    )


def embed_passage(
    text: str,
    *,
    timeout_seconds: float | None = None,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> EmbeddingResult:
    """One chunk through the passage model, for indexing."""
    return _embed(
        text,
        model=get_settings().upstage_embedding_model,
        timeout_seconds=timeout_seconds,
        transport=transport,
        sleep=sleep,
    )


def embed_query(
    text: str,
    *,
    timeout_seconds: float | None = None,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> EmbeddingResult:
    """One user question through the query model, for searching."""
    return _embed(
        text,
        model=get_settings().upstage_query_model,
        timeout_seconds=timeout_seconds,
        transport=transport,
        sleep=sleep,
    )


def _embed(
    text: str,
    *,
    model: str,
    timeout_seconds: float | None,
    transport: httpx.BaseTransport | None,
    sleep: Callable[[float], None],
) -> EmbeddingResult:
    """One text through one model.

    ``transport`` and ``sleep`` exist for the tests: a mock transport stands
    in for the API, and a recording sleep proves the backoff without waiting
    through it. Raises EmbeddingError for anything but a well-formed vector.
    """
    settings = get_settings()
    if not settings.upstage_api_key:
        raise EmbeddingError("UPSTAGE_API_KEY is not set; add it to .env")
    if not text or not text.strip():
        # The API rejects empty input; the caller marks these skipped and
        # should not get this far.
        raise EmbeddingError("there is no text to embed")

    timeout = (
        timeout_seconds if timeout_seconds is not None else settings.upstage_timeout_seconds
    )
    payload: dict[str, Any] = {"input": text, "model": model}

    failure = ""
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            sleep(BACKOFF_SECONDS * 2 ** (attempt - 1))
        try:
            with httpx.Client(transport=transport, timeout=timeout) as client:
                response = client.post(
                    settings.upstage_embedding_url,
                    headers={"Authorization": f"Bearer {settings.upstage_api_key}"},
                    json=payload,
                )
        except httpx.TimeoutException:
            failure = f"the embedding API did not answer within {timeout:.0f}s"
            continue
        except httpx.HTTPError as exc:
            failure = f"could not reach the embedding API: {exc.__class__.__name__}"
            continue

        if response.status_code == 200:
            return _read_result(response)
        if response.status_code in RETRYABLE_STATUSES:
            failure = f"embedding API returned HTTP {response.status_code}"
            continue
        # The request itself is at fault; retrying would repeat the mistake.
        raise EmbeddingError(
            f"embedding API returned HTTP {response.status_code}: {response.text[:200]}"
        )

    raise EmbeddingError(f"{failure} (after {MAX_RETRIES} retries)")
