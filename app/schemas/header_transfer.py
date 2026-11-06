from pydantic import BaseModel, ConfigDict, Field


class TocEntry(BaseModel):
    """One heading of the TOC an enrichment pipeline wrote into the metadata JSON.

    The pipeline nests sub-headings under ``children``; depth in that nesting is
    the heading's level. ``page_num`` is the printed page number, ``page_idx``
    and ``page_idx_end`` are content-list page indexes — the same three numbers
    the ``headers`` table stores as ``start_page`` / ``start_page_idx`` /
    ``end_page_idx``.
    """

    model_config = ConfigDict(extra="ignore")

    header: str
    page_num: int | None = None
    page_idx: int | None = None
    page_idx_end: int | None = None
    # Whether the pipeline located this heading in the content list; an
    # unmatched entry's indexes were inferred rather than observed.
    matched: bool = False
    children: list["TocEntry"] = Field(default_factory=list)


class TocMetadata(BaseModel):
    """The slice of ``{stem}_metadata.json`` the Transfer pop-up reads.

    The file also carries the base fields the Transform page wrote (``id``,
    ``filename``, ``pages``) plus the enrichment's own bookkeeping; anything not
    named here is ignored rather than rejected, so the pipeline can grow the
    file without breaking this reader.
    """

    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    filename: str | None = None
    pages: int | None = None
    has_toc: bool = False
    toc: list[TocEntry] = Field(default_factory=list)
    toc_confidence: float | None = None
    toc_method: str | None = None
    toc_valid: bool | None = None
    toc_warnings: list[str] = Field(default_factory=list)
