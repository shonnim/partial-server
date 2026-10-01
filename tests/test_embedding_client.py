"""The Upstage embedding client, against a mock transport — no real API.

The transport plays the API: success, rate limiting, server errors, client
errors, malformed bodies. The recorded sleeps prove the backoff without
waiting through it.
"""

import httpx
import pytest

from app.embedding.client import (
    BACKOFF_SECONDS,
    MAX_RETRIES,
    EmbeddingError,
    embed_passage,
)

OK_BODY = {
    "object": "list",
    "data": [{"object": "embedding", "index": 0, "embedding": [0.0068, -0.0359, 3.5e-38]}],
    "model": "solar-embedding-2-passage",
    "usage": {"prompt_tokens": 798, "total_tokens": 798},
}


def transport_of(*responses: httpx.Response) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """A transport that answers from a script and records every request."""
    requests: list[httpx.Request] = []
    script = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return script.pop(0) if len(script) > 1 else script[0]

    return httpx.MockTransport(handler), requests


def test_success_returns_vector_model_and_tokens() -> None:
    transport, requests = transport_of(httpx.Response(200, json=OK_BODY))
    result = embed_passage("채무 전망", transport=transport)

    assert result.vector == [0.0068, -0.0359, 3.5e-38]
    assert result.dimension == 3
    assert result.model == "solar-embedding-2-passage"
    assert result.prompt_tokens == 798

    # One request, with the key in the header and the text in the body.
    assert len(requests) == 1
    assert requests[0].headers["Authorization"].startswith("Bearer ")
    import json

    body = json.loads(requests[0].content)
    assert body["input"] == "채무 전망"
    assert body["model"]


def test_tiny_floats_are_kept_not_filtered() -> None:
    body = dict(OK_BODY, data=[{"object": "embedding", "index": 0, "embedding": [-3.5e-38]}])
    transport, _ = transport_of(httpx.Response(200, json=body))
    assert embed_passage("x", transport=transport).vector == [-3.5e-38]


def test_429_is_retried_with_backoff_then_succeeds() -> None:
    transport, requests = transport_of(
        httpx.Response(429, json={"error": "rate limited"}),
        httpx.Response(429, json={"error": "rate limited"}),
        httpx.Response(200, json=OK_BODY),
    )
    sleeps: list[float] = []
    result = embed_passage("x", transport=transport, sleep=sleeps.append)

    assert result.dimension == 3
    assert len(requests) == 3
    assert sleeps == [BACKOFF_SECONDS, BACKOFF_SECONDS * 2]


def test_persistent_server_error_gives_up_after_the_retries() -> None:
    transport, requests = transport_of(httpx.Response(503, text="down"))
    sleeps: list[float] = []

    with pytest.raises(EmbeddingError, match="HTTP 503"):
        embed_passage("x", transport=transport, sleep=sleeps.append)
    assert len(requests) == MAX_RETRIES + 1
    assert sleeps == [1.0, 2.0, 4.0]


def test_400_is_not_retried() -> None:
    transport, requests = transport_of(
        httpx.Response(400, json={"error": "input too long"})
    )
    with pytest.raises(EmbeddingError, match="HTTP 400"):
        embed_passage("x", transport=transport)
    assert len(requests) == 1


@pytest.mark.parametrize(
    "body",
    [
        {},  # nothing at all
        {"data": []},  # no embeddings
        {"data": [{"embedding": []}]},  # an empty vector
        {"data": [{"embedding": ["a", "b"]}]},  # not numbers
        {"data": [{"index": 0}]},  # no embedding field
    ],
)
def test_malformed_200_reply_raises(body: dict) -> None:
    transport, _ = transport_of(httpx.Response(200, json=body))
    with pytest.raises(EmbeddingError, match="no usable vector"):
        embed_passage("x", transport=transport)


def test_non_json_200_reply_raises() -> None:
    transport, _ = transport_of(httpx.Response(200, text="<html>oops</html>"))
    with pytest.raises(EmbeddingError, match="not JSON"):
        embed_passage("x", transport=transport)


def test_empty_text_is_refused_without_a_request() -> None:
    transport, requests = transport_of(httpx.Response(200, json=OK_BODY))
    with pytest.raises(EmbeddingError, match="no text"):
        embed_passage("   ", transport=transport)
    assert requests == []


def test_error_messages_never_carry_the_text_or_the_key() -> None:
    transport, _ = transport_of(httpx.Response(500, text="server exploded"))
    with pytest.raises(EmbeddingError) as info:
        embed_passage("비밀 본문", transport=transport, sleep=lambda _: None)
    assert "비밀 본문" not in str(info.value)
    assert "up_" not in str(info.value)
