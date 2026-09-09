"""A minimal client for the OpenAI-compatible chat endpoints.

Two calls are wrapped, and nothing more: a chat completion whose reply is
constrained to a JSON schema (the text model, for tables and outlines), and a
chat completion that carries an image (the vision model, for figures). Both
servers run vLLM, so a well-formed reply is the server's job and reading it
is ours.

Every failure — transport, HTTP status, malformed body — surfaces as LLMError
with a message worth showing a human. Nothing here retries: a retry would
silently double the cost of a slow call, and the caller is a person who can
click the button again.
"""

import base64
import json
from typing import Any

import httpx

from app.config import get_settings


class LLMError(RuntimeError):
    """The model could not be reached, or did not answer usefully."""


def _complete(
    payload: dict[str, Any], *, base_url: str, api_key: str, timeout: float
) -> str:
    """POST one chat completion and return the reply's text content.

    Raises LLMError for a transport failure, a non-200 status, a reply of an
    unexpected shape, or an empty reply.
    """
    url = f"{base_url.rstrip('/')}/chat/completions"

    try:
        response = httpx.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
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

    if not isinstance(content, str) or not content.strip():
        raise LLMError("the model returned an empty reply")

    return content


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

    content = _complete(
        payload,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        timeout=timeout,
    )

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise LLMError(f"the model's reply was not JSON: {exc.msg}") from exc

    if not isinstance(parsed, dict):
        raise LLMError("the model's reply was not a JSON object")

    return parsed


def chat_vision(
    *,
    system: str,
    image: bytes,
    media_type: str,
    text: str,
    max_tokens: int = 1024,
    temperature: float = 0.0,
    timeout_seconds: float | None = None,
) -> str:
    """One chat completion over an image and a text, answered as free text.

    The image goes inline as a base64 data URL: the vision server cannot reach
    this panel's URLs, and the bytes are what it needs anyway. Returns the
    reply's text. Raises LLMError for anything else.
    """
    settings = get_settings()
    timeout = timeout_seconds if timeout_seconds is not None else settings.vlm_timeout_seconds
    data_url = f"data:{media_type};base64,{base64.b64encode(image).decode('ascii')}"

    payload = {
        "model": settings.vlm_model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": text},
                ],
            },
        ],
    }

    return _complete(
        payload,
        base_url=settings.vlm_base_url,
        api_key=settings.vlm_api_key or settings.llm_api_key,
        timeout=timeout,
    )
