"""Turning a parsed image block into a searchable Korean description.

The parser leaves a figure as a crop in the run's ``images/`` folder, with
whatever caption and footnote text stood beside it on the page. Pixels are
not searchable; the vision model is asked to say what the figure shows, in
Korean, with the footnote folded in as context, and that description is what
``header_contents.text`` holds for the block.

The crop is read from RustFS and sent inline as base64: the vision server
has no way to reach this panel's image URLs. One call per image, no retry —
a description cannot be validated the way a table can, so there is nothing
to send back — and every failure raises ``ImageDescriptionError`` with the
reason: a crop missing from storage, a block naming no image, a model that
cannot be reached or answers empty. The caller decides what a failed image
means for the document.
"""

import posixpath
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.llm import LLMError, chat_vision
from app.models.document import Document
from app.services.parse_artifacts import ArtifactUnavailable, resolve_image
from app.services.table_normalizer import join_fragments
from app.storage import ObjectNotFound, read_object

# A page crop is a few hundred kilobytes; anything past this is not a figure
# the model should be asked about, and it is refused rather than sent.
MAX_IMAGE_BYTES = 20 * 1024 * 1024

# The description is a few paragraphs at most. Enough for a multi-panel
# figure; not so much that a rambling reply runs for minutes.
MAX_DESCRIPTION_TOKENS = 1024

SYSTEM_PROMPT = """You are a chart and figure description engine for a Korean-language document indexing and RAG pipeline.

Your task is to analyze the provided image and generate a detailed semantic description of what is represented in the image.

The final response MUST be written entirely in Korean.

The description will be stored as text and embedded into a vector database, so describe the image in a way that makes it useful for semantic search and RAG retrieval.

## Description requirements

1. Describe the overall subject and purpose of the image.
2. If the image contains multiple charts, figures, panels, or sections, describe each major part separately.
3. Explain what is being compared, measured, or illustrated.
4. Include important entities, countries, organizations, categories, concepts, and technical terms visible in the image.
5. Describe important axis labels, legends, time periods, units, and labels when they are readable.
6. Describe clearly visible trends, relationships, differences, or patterns when they are supported by the image.
7. Include important readable text from the image when it helps understand or retrieve the content.
8. Use the provided IMAGE FOOTNOTE as additional context. Preserve important source information, figure numbers, explanatory notes, definitions, and other relevant information from it.
9. Combine the visual information and IMAGE FOOTNOTE into one coherent description.
10. Do not blindly copy the entire footnote if it is not useful; integrate its meaningful information naturally into the description.
11. Do not attempt to extract every individual numerical point from a graph unless it is clearly readable.
12. Do not invent information that cannot be reliably determined from the image or footnote.
13. Do not provide OCR output alone. Produce a meaningful semantic description.
14. Make the description sufficiently detailed for RAG retrieval, including important concepts and keywords naturally in Korean.
15. Do not use Markdown tables.
16. Return ONLY the Korean description. Do not return JSON, headings, commentary, or explanations about the task.

The description should be several sentences or short paragraphs depending on the complexity of the image."""


class ImageDescriptionError(RuntimeError):
    """The image could not be described."""


@dataclass(frozen=True)
class DescribedImage:
    """One figure the model described."""

    text: str
    caption: str | None
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


def build_user_text(caption: str | None, footnote: str | None) -> str:
    """The text sent beside the image: what the page said about it.

    The system prompt speaks of an IMAGE FOOTNOTE, so that label is always
    present, and says so when the page gave none rather than leaving the
    model to wonder whether something was cut.
    """
    parts = []
    if caption:
        parts.append(f"IMAGE CAPTION:\n{caption}")
    parts.append(f"IMAGE FOOTNOTE:\n{footnote}" if footnote else "IMAGE FOOTNOTE:\n(none)")
    return "\n\n".join(parts)


def describe_image_block(
    db: Session, document: Document, block: dict[str, Any]
) -> DescribedImage:
    """Read one image block's crop from storage and have the model describe it.

    Raises ImageDescriptionError when the block names no image, the crop is
    not in storage or too large, or the model does not answer with a text.
    """
    name = image_name_of(block)
    if name is None:
        raise ImageDescriptionError("the block names no image file")

    try:
        image = resolve_image(db, document, name)
    except ArtifactUnavailable as exc:
        raise ImageDescriptionError(str(exc)) from exc

    try:
        raw, oversized = read_object(image.bucket, image.key, max_bytes=MAX_IMAGE_BYTES)
    except ObjectNotFound as exc:
        raise ImageDescriptionError(f"image missing from storage: {exc}") from exc
    if oversized:
        raise ImageDescriptionError(f"image {name} is larger than {MAX_IMAGE_BYTES} bytes")
    if not raw:
        raise ImageDescriptionError(f"image {name} is empty in storage")

    caption = join_fragments(block.get("image_caption"))
    footnote = join_fragments(block.get("image_footnote"))

    try:
        text = chat_vision(
            system=SYSTEM_PROMPT,
            image=raw,
            media_type=image.media_type,
            text=build_user_text(caption, footnote),
            max_tokens=MAX_DESCRIPTION_TOKENS,
        )
    except LLMError as exc:
        raise ImageDescriptionError(str(exc)) from exc

    return DescribedImage(text=text.strip(), caption=caption, image_name=name)
