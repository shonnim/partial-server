"""A minimal client for the OpenAI-compatible chat endpoint.

Only the one call this project makes is wrapped: a chat completion whose reply
is constrained to a JSON schema. The server runs vLLM, which enforces the
schema during decoding, so a well-formed reply is the server's job and parsing
it is ours.

Every failure — transport, HTTP status, malformed body — surfaces as LLMError
with a message worth showing a human. Nothing here retries: a retry would
silently double the cost of a slow call, and the caller is a person who can
click the button again.
"""

import json
from typing import Any

import httpx

from app.config import get_settings


class LLMError(RuntimeError):
    """The model could not be reached, or did not answer usefully."""


def chat_json(
    *,
    system: str,
    user: str,
    schema: dict[str, Any],
    schema_name: str,
    max_tokens: int = 8000,
    temperature: float = 0.0,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """One chat completion whose content is JSON matching ``schema``.

    ``timeout_seconds`` overrides the configured ceiling for a call known to
    run long. Returns the decoded object. Raises LLMError for anything else.
    """
    settings = get_settings()
    url = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
    timeout = timeout_seconds if timeout_seconds is not None else settings.llm_timeout_seconds

    payload = {
        "model": settings.llm_model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "schema": schema, "strict": True},
        },
    }

    try:
        response = httpx.post(
            url,
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
            json=payload,
            timeout=timeout,
        )
    except httpx.TimeoutException as exc:
        raise LLMError(f"the model did not answer within {timeout:.0f}s") from exc
    except httpx.HTTPError as exc:
        raise LLMError(f"could not reach the model: {exc}") from exc

    if response.status_code != 200:
        # The body is the only place vLLM says *why*, so a slice of it goes in
        # the message rather than a bare status code.
        raise LLMError(f"model returned HTTP {response.status_code}: {response.text[:300]}")

    try:
        content = response.json()["choices"][0]["message"]["content"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"unexpected reply shape from the model: {exc}") from exc

    if not content or not content.strip():
        raise LLMError("the model returned an empty reply")

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise LLMError(f"the model's reply was not JSON: {exc.msg}") from exc

    if not isinstance(parsed, dict):
        raise LLMError("the model's reply was not a JSON object")

    return parsed
