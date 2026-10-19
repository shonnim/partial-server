"""Turning a parsed figure — an image or a chart — into a short Korean summary.

The parser leaves a figure as a crop in the run's ``images/`` folder, with
whatever caption and footnote text stood beside it on the page and its own
reading of the figure: the text it could see in an image, or a table of the
values it read off a chart. Pixels are not searchable; the vision model is
shown the crop with all three as context and asked for four or five Korean
sentences — what the figure shows and what stands out in it — and that
summary is what ``header_contents.content`` holds for the block.

The crop is read from RustFS and sent inline as base64: the vision server
has no way to reach this panel's image URLs. One call per figure, no retry —
a summary cannot be validated the way a table can, so there is nothing to
send back — and every failure raises ``FigureDescriptionError`` with the
reason: a crop missing from storage, a block naming no image, a model that
cannot be reached or answers empty. The caller decides what a failed figure
means for the document.
"""

import posixpath
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.llm import LLMError, chat_vision
from app.models.document import Document
from app.services.parse_artifacts import ArtifactUnavailable, resolve_image
from app.services.parser_text import unescape
from app.services.table_normalizer import join_fragments
from app.storage import ObjectNotFound, read_object

# A page crop is a few hundred kilobytes; anything past this is not a figure
# the model should be asked about, and it is refused rather than sent.
MAX_IMAGE_BYTES = 20 * 1024 * 1024

# Four or five Korean sentences; the ceiling only stops a runaway reply.
MAX_DESCRIPTION_TOKENS = 512

# The block types this module describes. Each keeps its caption and footnote
# under its own keys in the parser's JSON.
FIGURE_TYPES = frozenset({"image", "chart"})

SYSTEM_PROMPT = """You are a figure summarisation engine for a Korean-language document indexing and RAG pipeline.

You are shown one figure — a chart, graph, diagram, or image — cropped from a page of a Korean report, together with the text the page printed beside it:

- CAPTION: the figure's title as printed, often with a figure number and units.
- FOOTNOTE: the notes and source printed under the figure.
- EXTRACTED CONTENT: what a document parser read off the figure — the text visible in an image, or a table of the values it read from a chart. It may be incomplete or slightly wrong; the picture is the authority.

Write a summary of the figure in Korean: 4 to 5 sentences, one paragraph.

- The first sentence says what the figure shows: the subject, what is being compared or measured, the entities (countries, groups, indicators), the period, and the units.
- The remaining sentences give the highlights: the clear trends, the largest and smallest values, turning points, differences between series or groups, and anything the caption or footnote singles out.
- Use the CAPTION and FOOTNOTE for the figure number, units, definitions, and source; fold that information into the sentences rather than quoting the whole footnote.
- Use the EXTRACTED CONTENT to name values precisely when it agrees with the picture; do not reproduce it as a table or a list.
- Name the important concepts and keywords in Korean so the summary is useful for semantic search; keep names that are printed in English as they are.
- Do not invent figures, trends, or sources that the image and the provided text do not support.
- Return ONLY the Korean paragraph: no Markdown, no headings, no JSON, no commentary about the task."""


class FigureDescriptionError(RuntimeError):
    """The figure could not be described."""


@dataclass(frozen=True)
class DescribedFigure:
    """One figure the model summarised."""

    text: str
    caption: str | None
    #: The footnote printed under the figure, as the page gave it.
    footnote: str | None
    #: The crop's file name inside the run's images folder.
    image_name: str


def image_name_of(block: dict[str, Any]) -> str | None:
    """The crop's file name from a block's ``img_path``, or None when it has none.

    The parser writes ``images/{digest}.jpg``; only the last segment is a
    name, and ``resolve_image`` is what decides whether that name is one it
    will build a key from.
    """
    path = block.get("img_path")
    if not isinstance(path, str) or not path.strip():
        return None
    name = posixpath.basename(path.strip())
    return name or None


def figure_texts(block: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """The caption, footnote and parser reading of a figure block.

    The caption and footnote live under ``image_caption`` / ``image_footnote``
    or ``chart_caption`` / ``chart_footnote`` by the block's type; the reading
    is ``content`` for both, with the parser's Markdown escaping taken off.
    """
    kind = block.get("type")
    caption = join_fragments(block.get(f"{kind}_caption"))
    footnote = join_fragments(block.get(f"{kind}_footnote"))
    content = block.get("content")
    reading = unescape(content) if isinstance(content, str) and content.strip() else None
    return caption, footnote, reading


def build_user_text(
    caption: str | None, footnote: str | None, content: str | None = None
) -> str:
    """The text sent beside the figure: what the page and the parser said about it.

    Every label the system prompt names is always present, and says so when
    there is nothing under it, rather than leaving the model to wonder
    whether something was cut.
    """
    return "\n\n".join(
        [
            f"CAPTION:\n{caption or '(none)'}",
            f"FOOTNOTE:\n{footnote or '(none)'}",
            f"EXTRACTED CONTENT:\n{content or '(none)'}",
        ]
    )


def describe_figure_block(
    db: Session, document: Document, block: dict[str, Any]
) -> DescribedFigure:
    """Read one figure block's crop from storage and have the model summarise it.

    Raises FigureDescriptionError when the block names no image, the crop is
    not in storage or too large, or the model does not answer with a text.
    """
    name = image_name_of(block)
    if name is None:
        raise FigureDescriptionError("the block names no image file")

    try:
        image = resolve_image(db, document, name)
    except ArtifactUnavailable as exc:
        raise FigureDescriptionError(str(exc)) from exc

    try:
        raw, oversized = read_object(image.bucket, image.key, max_bytes=MAX_IMAGE_BYTES)
    except ObjectNotFound as exc:
        raise FigureDescriptionError(f"image missing from storage: {exc}") from exc
    if oversized:
        raise FigureDescriptionError(f"image {name} is larger than {MAX_IMAGE_BYTES} bytes")
    if not raw:
        raise FigureDescriptionError(f"image {name} is empty in storage")

    caption, footnote, reading = figure_texts(block)

    try:
        text = chat_vision(
            system=SYSTEM_PROMPT,
            image=raw,
            media_type=image.media_type,
            text=build_user_text(caption, footnote, reading),
            max_tokens=MAX_DESCRIPTION_TOKENS,
        )
    except LLMError as exc:
        raise FigureDescriptionError(str(exc)) from exc
    if not text.strip():
        raise FigureDescriptionError("the model answered with an empty text")

    return DescribedFigure(
        text=text.strip(), caption=caption, footnote=footnote, image_name=name
    )
