"""A minimal client for the document parsing service.

Only one call is wrapped: parse a page range of a PDF with the VLM backend and
hand back the markdown it produced. The service parses whole documents too,
but this project only ever asks it for a contents page — the full parse is
done elsewhere and its output is already in RustFS.

Every failure — transport, HTTP status, a body without markdown in it — comes
back as DocExtractError with a message worth showing a human. Nothing here
retries: one call is minutes of VLM time, and the caller is a person who can
click the button again.
"""

import json
from typing import Any

import httpx

from app.config import get_settings

# The parse mode the contents page is read with. The VLM path reads layout
# (indentation, dot leaders, page numbers) far better than the OCR pipeline,
# and layout is the only thing a contents page has.
BACKEND = "vlm-engine"


class DocExtractError(RuntimeError):
    """The parsing service could not be reached, or did not answer usefully."""


def _find_markdown(results: Any) -> str | None:
    """The ``md_content`` of the one file that was sent.

    Results are keyed by the file's stem, which the service derives from the
    filename it was given. Rather than reproduce that derivation here — and
    silently miss when it changes — the single entry is taken whatever it is
    called.
    """
    if not isinstance(results, dict):
        return None
    for entry in results.values():
        if isinstance(entry, dict) and isinstance(entry.get("md_content"), str):
            return entry["md_content"]
    return None


def parse_pages(
    pdf: bytes, filename: str, *, start_page: int, end_page: int
) -> str:
    """Parse pages ``start_page``..``end_page`` (0-based, inclusive) to markdown.

    Returns the markdown. Raises DocExtractError for anything else, including
    a reply that parsed nothing — an empty page is not a contents page.
    """
    if start_page < 0 or end_page < start_page:
        raise DocExtractError(
            f"page range {start_page}..{end_page} is not valid"
        )

    settings = get_settings()
    url = f"{settings.doc_extract_base_url.rstrip('/')}/file_parse"

    try:
        response = httpx.post(
            url,
            files={"files": (filename, pdf, "application/pdf")},
            data={
                "backend": BACKEND,
                "return_md": "true",
                "start_page_id": str(start_page),
                "end_page_id": str(end_page),
            },
            timeout=settings.doc_extract_timeout_seconds,
        )
    except httpx.TimeoutException as exc:
        raise DocExtractError(
            "the parsing service did not answer within "
            f"{settings.doc_extract_timeout_seconds:.0f}s"
        ) from exc
    except httpx.HTTPError as exc:
        raise DocExtractError(f"could not reach the parsing service: {exc}") from exc

    if response.status_code != 200:
        raise DocExtractError(
            f"parsing service returned HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )

    try:
        body = response.json()
    except json.JSONDecodeError as exc:
        raise DocExtractError(
            f"the parsing service's reply was not JSON: {exc.msg}"
        ) from exc

    if not isinstance(body, dict):
        raise DocExtractError("the parsing service's reply was not a JSON object")

    markdown = _find_markdown(body.get("results"))
    if markdown is None:
        status = body.get("status")
        raise DocExtractError(
            f"the parsing service returned no markdown (status: {status!r})"
        )
    if not markdown.strip():
        raise DocExtractError(
            f"pages {start_page}–{end_page} parsed to empty markdown"
        )

    return markdown
