"""The chunking strategy for group ``imf-1``: hierarchical, by outline level.

Every document in the group is a KIPF summary of an IMF or OECD report and
is laid out the same way after its table of contents:

    I.   Part            (roman numeral)        ─┐
    1.   Chapter         (arabic numeral)        ├─ chunk boundaries and breadcrumb
    가.  Section         (hangul, often absent) ─┘
    □ ○ ∙ -              body bullets, four levels deep — content, never a boundary

A *section* is the deepest of the three heading levels in force, and a chunk
never crosses one. Every chunk opens with the breadcrumb of the headings
above it, ``[I. … > 1. … > 가. …]``, so a chunk read on its own still says
where in the report it came from.

The rows come from ``header_contents`` in id order. Before anything is
filtered, page footnotes are folded back into the text that cites them: a
``page_footnote`` row ``1) 일반정부, 가계, 비금융기업`` replaces the ``1)``
glued to a word on the same page with ``(일반정부, 가계, 비금융기업)``. Then
rows that belong to no header and rows that merely repeat their header's
title are dropped, and what is left is grouped:

* text and list rows are joined within a section and cut to a token budget
  at row boundaries, by preference right before a ``□`` line so a bullet
  keeps its children;
* a table is always its own chunk, as Markdown, caption and footnote around
  it;
* an image or chart is always its own chunk, the same way.

The token budget counts the breadcrumb. Tokens are counted by the function
passed in, so the strategy can be exercised without loading a tokenizer.
"""

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from app.models.header import Header, HeaderContent
from app.services.html_table import TableParseError, html_table_to_markdown
from app.services.tokenizer import count_tokens as _real_count_tokens

# Token budget for a text chunk, breadcrumb included.
TARGET_TOKENS = 600
MIN_TOKENS = 400
MAX_TOKENS = 800
# A table is kept whole up to this; past it the chunk is saved as it is and
# flagged, see ``DraftChunk.needs_split``.
MAX_TABLE_TOKENS = 900

# Rows whose content is prose: joined into text chunks.
TEXT_TYPES = frozenset({"text", "list"})
# Rows that always stand alone as a chunk.
STANDALONE_TYPES = frozenset({"table", "image", "chart"})

# A heading as a plain text row is short and on one line; anything longer is
# a paragraph that happens to open with a number.
MAX_INLINE_HEADING_CHARS = 150

# ---- outline levels ----------------------------------------------------------

_ROMAN = r"(?:[IVX]{1,5}|[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ])"
# "I. 채무의 현명한 관리" — a roman numeral, a dot, then the title. The
# lookahead keeps "I.V." and "I.1" out; Hangul right after the dot is fine.
_LEVEL_1 = re.compile(rf"^\s*{_ROMAN}\s*\.(?![A-Za-z0-9)])")
# "1. 세계 채무 현황", also "2.채무 수준" with the space missing. The digit
# lookahead is what keeps "1.5%" from reading as a chapter.
_LEVEL_2 = re.compile(r"^\s*\d{1,2}\s*\.(?!\d)")
# "가. …" through "하. …".
_LEVEL_3 = re.compile(r"^\s*[가나다라마바사아자차카타파하]\s*\.(?!\d)")

_LEVEL_PATTERNS = ((1, _LEVEL_1), (2, _LEVEL_2), (3, _LEVEL_3))

# A body bullet at the top level of the bullet hierarchy: where a text chunk
# prefers to be cut. The parser reads the box as □ or as ☐.
_BOX_BULLET = re.compile(r"^\s*[□☐]")


def detect_level(text: str | None) -> int | None:
    """Which of the three outline levels a heading is, or None for neither."""
    if not text:
        return None
    for level, pattern in _LEVEL_PATTERNS:
        if pattern.match(text):
            return level
    return None


def _squash(text: str | None) -> str:
    """Text with every run of whitespace removed, for comparing titles."""
    return re.sub(r"\s+", "", text or "")


def _ancestry(headers: Mapping[int, Header], header_id: int | None) -> list[Header]:
    """A header and its parents, outermost first; empty for an unknown id."""
    chain: list[Header] = []
    seen: set[int] = set()
    header = headers.get(header_id) if header_id is not None else None
    while header is not None and header.id not in seen:
        seen.add(header.id)
        chain.append(header)
        header = headers.get(header.parent_id) if header.parent_id is not None else None
    chain.reverse()
    return chain


# ---- footnotes ---------------------------------------------------------------

# "1) text 2) more text" → the markers and what follows each. A row may hold
# several footnotes, each starting at a marker that opens the row or follows
# whitespace.
_FOOTNOTE_SPLIT = re.compile(r"(?:^|(?<=\s))(\d{1,2})\)\s*")


@dataclass(frozen=True)
class Footnote:
    marker: str
    text: str


def parse_footnotes(content: str | None) -> list[Footnote]:
    """The footnotes in one ``page_footnote`` row, in printed order.

    Text before the first marker — a row without any marker at all — comes
    back with an empty marker, so it can still be kept rather than lost.
    """
    if not content or not content.strip():
        return []
    parts = _FOOTNOTE_SPLIT.split(content)
    notes: list[Footnote] = []
    lead = parts[0].strip()
    if lead:
        notes.append(Footnote(marker="", text=lead))
    for i in range(1, len(parts) - 1, 2):
        text = parts[i + 1].strip()
        if text:
            notes.append(Footnote(marker=parts[i], text=text))
    return notes


_SUPERSCRIPT = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")


def reference_pattern(marker: str) -> re.Pattern[str]:
    """Where ``marker)`` cites a footnote inside prose.

    The parser prints a citation several ways: glued to the word before it
    ``비금융부문1)``, as superscript digits ``선진국¹)``, and as LaTeX
    ``$^{2}$ )``, ``$^{3)}$`` or a bare ``$^{4}$`` with the paren lost. The
    plain form must follow a character that is not whitespace, not an opening
    parenthesis (``(1)`` is an enumeration, not a citation) and not a digit
    (``11)`` is not a citation of footnote 1).
    """
    plain = re.escape(marker)
    superscript = re.escape(marker.translate(_SUPERSCRIPT))
    return re.compile(
        rf"(?<=[^\s(\d]){plain}\)"
        rf"|(?<=[^\s(]){superscript}\)"
        rf"|\$\^\{{{plain}\}}\$\s*\)"
        rf"|\$\^\{{{plain}\)\}}\$"
        rf"|\$\^\{{{plain}\}}\$"
    )


def inline_footnote(text: str, note: Footnote) -> str | None:
    """``text`` with the first citation of ``note`` replaced by its text in
    parentheses, or None when the text does not cite it."""
    if not note.marker:
        return None
    match = reference_pattern(note.marker).search(text)
    if match is None:
        return None
    return f"{text[: match.start()]}({note.text}){text[match.end() :]}"


def appended_footnote(text: str, note: Footnote) -> str:
    """The fallback when nothing cites the footnote: hung off the end of a row."""
    label = f"({note.marker}) {note.text})" if note.marker else f"({note.text})"
    return f"{text} {label}"


# ---- the output ----------------------------------------------------------------


@dataclass
class DraftChunk:
    """One chunk as the strategy hands it back, before it is saved."""

    text: str
    chunk_type: str
    section_path: str
    token_count: int
    # ids of every header_contents row in the chunk, inlined footnotes included.
    head_content_ids: list[int]
    # A table past the table budget, saved whole and flagged for later.
    needs_split: bool = False


@dataclass
class StrategyResult:
    chunks: list[DraftChunk] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---- the walk ------------------------------------------------------------------


@dataclass
class _Row:
    """A content row after footnotes are folded in: what the walk reads."""

    id: int
    header_id: int | None
    type: str
    content: str
    caption: str | None
    footnote: str | None
    page_idx: int | None
    # The footnote rows folded into this one, so the chunk can list them.
    extra_ids: list[int] = field(default_factory=list)

    @property
    def ids(self) -> list[int]:
        return [self.id, *self.extra_ids]

    def add_source(self, row_id: int) -> None:
        """Note a footnote row folded in — once, however many notes it gave."""
        if row_id not in self.extra_ids:
            self.extra_ids.append(row_id)

    @property
    def is_blank(self) -> bool:
        if self.type in STANDALONE_TYPES:
            return not any(
                part and part.strip() for part in (self.content, self.caption, self.footnote)
            )
        return not self.content.strip()


@dataclass
class _Path:
    """The headings in force, one slot per outline level."""

    titles: list[str | None] = field(default_factory=lambda: [None, None, None])

    def set(self, level: int, title: str) -> None:
        self.titles[level - 1] = title
        for deeper in range(level, 3):
            self.titles[deeper] = None

    @property
    def key(self) -> tuple[str | None, ...]:
        return tuple(self.titles)

    @property
    def breadcrumb(self) -> str:
        return "[" + " > ".join(t for t in self.titles if t) + "]"

    @property
    def is_empty(self) -> bool:
        return not any(self.titles)


@dataclass
class _Piece:
    """A run of text that is never cut: a row, or part of an over-long row."""

    text: str
    tokens: int
    ids: list[int]
    # Position of the row in the walk, which is where the chunk it opens goes.
    seq: int


@dataclass
class _Buffer:
    """Text rows waiting to become chunks: one section's worth at most."""

    key: tuple[str | None, ...]
    breadcrumb: str
    rows: list[tuple[int, _Row]] = field(default_factory=list)


class ImfHierarchicalChunker:
    """The ``imf-1`` strategy. See the module docstring."""

    def __init__(self, count_tokens: Callable[[str], int] = _real_count_tokens) -> None:
        self.count_tokens = count_tokens

    def chunk(
        self, rows: Sequence[HeaderContent], headers: Mapping[int, Header]
    ) -> StrategyResult:
        result = StrategyResult()
        prepared = self._prepare(rows, result.warnings)
        kept = self._filter(prepared, headers)

        path = _Path()
        warned_no_path = False
        warned_paths: set[tuple[str | None, ...]] = set()
        warned_headers: set[int] = set()
        current_header: int | None = None
        buffer: _Buffer | None = None
        # (position, chunk): text chunks take the position of their first row,
        # so a table between two text rows keeps its place between them.
        placed: list[tuple[int, DraftChunk]] = []

        def flush() -> None:
            nonlocal buffer
            if buffer is not None and buffer.rows:
                placed.extend(self._text_chunks(buffer, result.warnings))
            buffer = None

        for seq, row in enumerate(kept):
            if row.header_id != current_header:
                current_header = row.header_id
                # The header and its ancestors, top down. A part whose only
                # row is its own title never reaches this loop, so the path
                # is rebuilt from the outline rather than from rows seen.
                for header in _ancestry(headers, row.header_id):
                    level = detect_level(header.header)
                    if level is None:
                        # The outline's own depth is the next best guide.
                        level = min(max(header.level, 1), 3)
                        if header.id not in warned_headers:
                            warned_headers.add(header.id)
                            result.warnings.append(
                                f"header {header.id} '{header.header}' matches no "
                                f"I./1./가. marker; treated as level {level}"
                            )
                    path.set(level, header.header.strip())

            if self._is_inline_heading(row):
                # A heading the outline missed, printed as plain text. It moves
                # the path and is not content.
                level = detect_level(row.content)
                if level is not None:
                    path.set(level, row.content.strip())
                continue

            if path.is_empty:
                if not warned_no_path:
                    warned_no_path = True
                    result.warnings.append(
                        "content before the first I./1. heading has no breadcrumb"
                    )
            elif path.titles[0] and not path.titles[1] and path.key not in warned_paths:
                warned_paths.add(path.key)
                result.warnings.append(
                    f"content directly under '{path.titles[0]}' with no chapter (1., 2.) heading"
                )

            if buffer is not None and buffer.key != path.key:
                flush()

            if row.type in STANDALONE_TYPES:
                placed.append(
                    (seq, self._standalone_chunk(row, path.breadcrumb, result.warnings))
                )
                continue

            if buffer is None:
                buffer = _Buffer(key=path.key, breadcrumb=path.breadcrumb)
            buffer.rows.append((seq, row))

        flush()
        placed.sort(key=lambda item: item[0])
        result.chunks = [chunk for _, chunk in placed]
        return result

    # ---- preparation ---------------------------------------------------------

    def _prepare(self, rows: Sequence[HeaderContent], warnings: list[str]) -> list[_Row]:
        """Rows as ``_Row``, with every page footnote folded into its citation."""
        prepared = [
            _Row(
                id=r.id,
                header_id=r.header_id,
                type=r.type,
                content=r.content or "",
                caption=r.caption,
                footnote=r.footnote,
                page_idx=r.page_idx,
            )
            for r in rows
        ]

        for index, row in enumerate(prepared):
            if row.type != "page_footnote":
                continue
            for note in parse_footnotes(row.content):
                self._place_footnote(prepared, index, row, note, warnings)
        # A footnote row never becomes content on its own.
        return [row for row in prepared if row.type != "page_footnote"]

    @staticmethod
    def _place_footnote(
        rows: list[_Row], index: int, footnote_row: _Row, note: Footnote, warnings: list[str]
    ) -> None:
        same_page = [
            r for r in rows if r.type in TEXT_TYPES and r.page_idx == footnote_row.page_idx
        ]
        for target in same_page:
            replaced = inline_footnote(target.content, note)
            if replaced is not None:
                target.content = replaced
                target.add_source(footnote_row.id)
                return

        # Not cited anywhere on the page: hang it off the nearest text row
        # before it on the same page, failing that the nearest before it at all.
        preceding = [r for r in rows[:index] if r.type in TEXT_TYPES]
        fallback = next(
            (r for r in reversed(preceding) if r.page_idx == footnote_row.page_idx), None
        ) or (preceding[-1] if preceding else None)
        marker = f"{note.marker})" if note.marker else "(no marker)"
        if fallback is None:
            warnings.append(
                f"footnote {marker} on page_idx {footnote_row.page_idx} (row {footnote_row.id}) "
                "cites nothing and no text row precedes it; dropped"
            )
            return
        fallback.content = appended_footnote(fallback.content, note)
        fallback.add_source(footnote_row.id)
        warnings.append(
            f"footnote {marker} on page_idx {footnote_row.page_idx} (row {footnote_row.id}) "
            f"is not cited on that page; appended to row {fallback.id}"
        )

    @staticmethod
    def _filter(rows: list[_Row], headers: Mapping[int, Header]) -> list[_Row]:
        """Drop rows outside any header, rows that only repeat their heading,
        and rows with nothing in them."""
        kept: list[_Row] = []
        for row in rows:
            if row.header_id is None or row.is_blank:
                continue
            header = headers.get(row.header_id)
            if (
                header is not None
                and row.type in TEXT_TYPES
                and _squash(row.content) == _squash(header.header)
            ):
                continue
            kept.append(row)
        return kept

    @staticmethod
    def _is_inline_heading(row: _Row) -> bool:
        text = row.content.strip()
        return (
            row.type == "text"
            and "\n" not in text
            and len(text) <= MAX_INLINE_HEADING_CHARS
            and detect_level(text) is not None
        )

    # ---- text chunks -----------------------------------------------------------

    def _text_chunks(
        self, buffer: _Buffer, warnings: list[str]
    ) -> list[tuple[int, DraftChunk]]:
        """Cut one section's text rows into chunks within the token budget."""
        prefix = buffer.breadcrumb + "\n"
        prefix_tokens = self.count_tokens(prefix)
        budget = MAX_TOKENS - prefix_tokens

        pieces: list[_Piece] = []
        for seq, row in buffer.rows:
            text = row.content.strip()
            tokens = self.count_tokens(text)
            if tokens <= budget:
                pieces.append(_Piece(text, tokens, row.ids, seq))
                continue
            warnings.append(
                f"row {row.id} is {tokens} tokens on its own; split by lines and sentences"
            )
            for i, part in enumerate(self._split_long(text, budget)):
                # The row's ids ride with its first part only, once per row.
                pieces.append(_Piece(part, self.count_tokens(part), row.ids if i == 0 else [], seq))

        groups = self._group(pieces, prefix_tokens)
        chunks = [self._text_chunk(group, buffer.breadcrumb) for group in groups]

        # A short tail is folded back into the chunk before it when that fits.
        if len(chunks) >= 2 and chunks[-1][1].token_count < MIN_TOKENS:
            merged = self._text_chunk(groups[-2] + groups[-1], buffer.breadcrumb)
            if merged[1].token_count <= MAX_TOKENS:
                chunks[-2:] = [merged]
        return chunks

    @staticmethod
    def _group(pieces: list[_Piece], prefix_tokens: int) -> list[list[_Piece]]:
        """Greedy packing to the budget. Every piece costs its tokens plus the
        newline that joins it. Past the target, a ``□`` line opens a new
        chunk; past the maximum, the chunk is cut before its last ``□`` line
        when that leaves enough behind, and otherwise right here."""
        groups: list[list[_Piece]] = []
        current: list[_Piece] = []
        current_tokens = prefix_tokens

        def cost(piece: _Piece) -> int:
            return piece.tokens + 1

        for piece in pieces:
            if current and current_tokens + cost(piece) > MAX_TOKENS:
                head, tail = ImfHierarchicalChunker._cut_at_box(current, prefix_tokens)
                groups.append(head)
                current = tail
                current_tokens = prefix_tokens + sum(cost(p) for p in current)
                if current and current_tokens + cost(piece) > MAX_TOKENS:
                    groups.append(current)
                    current, current_tokens = [], prefix_tokens
            elif (
                current and current_tokens >= TARGET_TOKENS and _BOX_BULLET.match(piece.text)
            ):
                groups.append(current)
                current, current_tokens = [], prefix_tokens
            current.append(piece)
            current_tokens += cost(piece)
        if current:
            groups.append(current)
        return groups

    @staticmethod
    def _cut_at_box(
        current: list[_Piece], prefix_tokens: int
    ) -> tuple[list[_Piece], list[_Piece]]:
        """Split a full group before its last ``□`` line, if the part before
        it is still a chunk's worth; otherwise take the whole group."""
        running = prefix_tokens
        best = len(current)
        for i, piece in enumerate(current):
            if i > 0 and _BOX_BULLET.match(piece.text) and running >= MIN_TOKENS:
                best = i
            running += piece.tokens + 1
        return current[:best], current[best:]

    def _text_chunk(self, group: list[_Piece], breadcrumb: str) -> tuple[int, DraftChunk]:
        text = breadcrumb + "\n" + "\n".join(p.text for p in group)
        chunk = DraftChunk(
            text=text,
            chunk_type="text",
            section_path=breadcrumb,
            token_count=self.count_tokens(text),
            head_content_ids=[i for p in group for i in p.ids],
        )
        return group[0].seq, chunk

    def _split_long(self, text: str, budget: int) -> list[str]:
        """Cut one over-long row into pieces within ``budget``: by lines, then
        sentences, then words as a last resort."""
        units: list[str] = []
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            if self.count_tokens(line) <= budget:
                units.append(line)
                continue
            for sentence in re.split(r"(?<=[.!?。])\s+", line):
                if self.count_tokens(sentence) <= budget:
                    units.append(sentence)
                else:
                    units.extend(self._split_words(sentence, budget))
        return self._pack(units, budget, "\n")

    def _split_words(self, text: str, budget: int) -> list[str]:
        return self._pack(text.split(), budget, " ")

    def _pack(self, units: list[str], budget: int, joiner: str) -> list[str]:
        pieces: list[str] = []
        current: list[str] = []
        current_tokens = 0
        for unit in units:
            tokens = self.count_tokens(unit) + 1
            if current and current_tokens + tokens > budget:
                pieces.append(joiner.join(current))
                current, current_tokens = [], 0
            current.append(unit)
            current_tokens += tokens
        if current:
            pieces.append(joiner.join(current))
        return pieces

    # ---- standalone chunks ------------------------------------------------------

    def _standalone_chunk(
        self, row: _Row, breadcrumb: str, warnings: list[str]
    ) -> DraftChunk:
        content = row.content.strip()
        if row.type == "table" and content:
            # Stored as the parser's HTML; the embedder gets Markdown.
            try:
                content = html_table_to_markdown(content)
            except TableParseError as exc:
                warnings.append(
                    f"table row {row.id}: HTML could not be read ({exc}); kept as HTML"
                )

        parts = [breadcrumb]
        for part in (row.caption, content, row.footnote):
            if part and part.strip():
                parts.append(part.strip())
        text = "\n".join(parts)

        chunk = DraftChunk(
            text=text,
            chunk_type=row.type,
            section_path=breadcrumb,
            token_count=self.count_tokens(text),
            head_content_ids=row.ids,
        )
        # TODO: split a table past the budget by rows, repeating its header
        # row in every part. For now it is saved whole and flagged.
        if row.type == "table" and chunk.token_count > MAX_TABLE_TOKENS:
            chunk.needs_split = True
            warnings.append(
                f"table row {row.id} is {chunk.token_count} tokens, over {MAX_TABLE_TOKENS}; "
                "saved as one chunk (needs_split)"
            )
        return chunk
