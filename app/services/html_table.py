"""Turning a parsed table's HTML into Markdown, deterministically.

The parser hands a table over as HTML (``table_body``) with the caption and
footnotes beside it. The HTML already says where every cell sits — including
the cells that span several rows or columns — so the logical table can be
rebuilt from it in plain Python, with no model and no network. Tens of
thousands of tables go through here; each one is parsed once and walked
once.

The steps, in order:

1. parse the HTML into rows of cells, each with its text and spans;
2. expand ``rowspan`` and ``colspan`` into a rectangular grid where every
   position points at the cell that covers it;
3. decide which leading rows are the header (``<thead>``, ``<th>``, spans,
   year-like values — see ``detect_header_rows``);
4. flatten a multi-row header into one label per column, levels joined with
   " / ", a cell that spans several header rows counted once;
5. render the Markdown table, then put the caption above it and the
   footnotes below.

Nothing is invented. A cell's text is kept as written apart from collapsing
its whitespace; an empty cell stays empty; an empty header corner stays
empty; a row that is one cell spanning the whole table — "Memorandum" —
stays as that one label with empty cells after it. A table whose HTML cannot
be read safely raises ``TableParseError`` with the reason, and the caller
decides what that means; no Markdown is produced from a table that might be
missing part of itself.
"""

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

# What joins the levels of a multi-row header into one column label.
HEADER_LEVEL_JOIN = " / "

# What joins the lines of a caption. The parser splits a caption over its
# printed lines — title, then "(단위: %)" — and they read as one line.
CAPTION_JOIN = " "

FOOTNOTES_HEADING = "### Footnotes"

# A span larger than this is not a table anyone laid out; it is a parser
# artefact, and expanding it would allocate a grid nobody wants.
MAX_SPAN = 1000

_WHITESPACE = re.compile(r"\s+")

# A plain number as a cell value: an optional sign, digits with optional
# thousands separators, an optional fraction, an optional trailing percent
# sign. Anything else — words, years with suffixes, "+", "..." — is not.
_NUMBER = re.compile(r"^[-+−–]?\(?\d{1,3}(?:,\d{3})*(?:\.\d+)?\)?%?$|^[-+−–]?\(?\d+(?:\.\d+)?\)?%?$")

# A cell that names a year: 1900–2099, optionally followed by a short suffix
# such as "(전망)", "e", "p", "Q1". Years are what a header row of a
# statistical table is usually made of.
_YEAR = re.compile(r"^(?:FY)?(?:19|20)\d{2}(?:[/\-–~]\d{2,4})?(?:\s*\S{0,8})?$")
_BARE_YEAR = re.compile(r"^(?:19|20)\d{2}$")


class TableParseError(ValueError):
    """The HTML could not be turned into a table worth keeping."""


# ---- reading the HTML --------------------------------------------------------


@dataclass(eq=False)
class Cell:
    """One ``td``/``th`` as written, before spans are expanded.

    Compared by identity: two grid positions covered by the same cell hold
    the same object, which is how a spanning header is counted once.
    """

    text: str
    rowspan: int = 1
    colspan: int = 1
    is_header: bool = False


@dataclass
class Row:
    cells: list[Cell] = field(default_factory=list)
    in_thead: bool = False


def _span(attrs: dict[str, str | None], name: str) -> int:
    raw = attrs.get(name)
    if raw is None:
        return 1
    try:
        value = int(str(raw).strip())
    except ValueError:
        return 1
    if value < 1:
        # rowspan="0" means "to the end of the section" in HTML; that is not
        # something these tables use, and treating it as 1 keeps the cell.
        return 1
    if value > MAX_SPAN:
        raise TableParseError(f"{name}={value} is larger than any real table")
    return value


class _TableReader(HTMLParser):
    """Rows of cells in document order, entities decoded, one table only."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[Row] = []
        self._depth = 0  # how many <table> tags are open
        self._row: Row | None = None
        self._cell: Cell | None = None
        self._parts: list[str] = []
        self._in_thead = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._depth += 1
            if self._depth > 1:
                raise TableParseError("nested tables are not supported")
            return
        if tag == "thead":
            self._in_thead = True
        elif tag == "tr":
            self._close_cell()
            self._row = Row(in_thead=self._in_thead)
            self.rows.append(self._row)
        elif tag in ("td", "th"):
            self._close_cell()
            if self._row is None:
                raise TableParseError(f"<{tag}> outside of a <tr>")
            attributes = dict(attrs)
            self._cell = Cell(
                text="",
                rowspan=_span(attributes, "rowspan"),
                colspan=_span(attributes, "colspan"),
                is_header=(tag == "th"),
            )
            self._parts = []
        elif tag in ("br", "p", "div") and self._cell is not None:
            self._parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th"):
            self._close_cell()
        elif tag == "tr":
            self._close_cell()
            self._row = None
        elif tag == "thead":
            self._in_thead = False
        elif tag == "table":
            self._close_cell()
            self._row = None
            self._depth = max(0, self._depth - 1)

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._parts.append(data)

    def _close_cell(self) -> None:
        if self._cell is None:
            return
        self._cell.text = _WHITESPACE.sub(" ", "".join(self._parts)).strip()
        assert self._row is not None
        self._row.cells.append(self._cell)
        self._cell = None
        self._parts = []

    def finish(self) -> list[Row]:
        self.close()
        self._close_cell()
        return self.rows


def parse_html_table(table_html: str) -> list[Row]:
    """The rows of the table as written, spans not yet expanded.

    Raises TableParseError when there is no table to read: no rows, a cell
    outside a row, a table inside a table.
    """
    if not isinstance(table_html, str) or not table_html.strip():
        raise TableParseError("the table HTML is empty")
    reader = _TableReader()
    try:
        reader.feed(table_html)
        rows = reader.finish()
    except TableParseError:
        raise
    except Exception as exc:  # html.parser is lenient; anything else is news
        raise TableParseError(f"the HTML could not be parsed: {exc}") from exc
    rows = [row for row in rows if row.cells]
    if not rows:
        raise TableParseError("the HTML holds no table rows")
    return rows


# ---- the logical grid --------------------------------------------------------


@dataclass
class Grid:
    """The table with every span expanded.

    ``cells[r][c]`` is the Cell covering that position, or None where no
    cell was written (a short row). ``origin[r][c]`` is True where the cell
    starts — its top-left position — so a spanning cell can be told apart
    from the positions it merely covers.
    """

    cells: list[list[Cell | None]]
    origin: list[list[bool]]
    in_thead: list[bool]

    @property
    def height(self) -> int:
        return len(self.cells)

    @property
    def width(self) -> int:
        return len(self.cells[0]) if self.cells else 0


def build_grid(rows: list[Row]) -> Grid:
    """Expand rowspan and colspan into a rectangular grid.

    The standard table layout walk: each row's cells take the leftmost
    positions not already covered by a span from above, in order. A rowspan
    that runs past the last row is cut at the table's end rather than
    conjuring rows. Every cell keeps its place; none is dropped.
    """
    height = len(rows)
    cells: list[list[Cell | None]] = [[] for _ in range(height)]
    origin: list[list[bool]] = [[] for _ in range(height)]

    def ensure_width(r: int, width: int) -> None:
        while len(cells[r]) < width:
            cells[r].append(None)
            origin[r].append(False)

    for r, row in enumerate(rows):
        c = 0
        for cell in row.cells:
            ensure_width(r, c + 1)
            while cells[r][c] is not None:
                c += 1
                ensure_width(r, c + 1)
            for dr in range(min(cell.rowspan, height - r)):
                ensure_width(r + dr, c + cell.colspan)
                for dc in range(cell.colspan):
                    if cells[r + dr][c + dc] is None:
                        cells[r + dr][c + dc] = cell
                        origin[r + dr][c + dc] = dr == 0 and dc == 0
            c += cell.colspan

    width = max((len(line) for line in cells), default=0)
    for r in range(height):
        ensure_width(r, width)

    return Grid(cells=cells, origin=origin, in_thead=[row.in_thead for row in rows])


# ---- the header --------------------------------------------------------------


def _is_number(text: str) -> bool:
    return bool(_NUMBER.match(text))


def _is_year(text: str) -> bool:
    """A bare year, or a year with a short suffix — never a decimal number."""
    if _BARE_YEAR.match(text):
        return True
    return bool(_YEAR.match(text)) and not _is_number(text)


def _row_texts(grid: Grid, r: int) -> list[str]:
    return [cell.text if cell is not None else "" for cell in grid.cells[r]]


def _is_full_width_row(grid: Grid, r: int) -> bool:
    """One cell covering the whole row: a section heading, never a header."""
    first = grid.cells[r][0] if grid.width else None
    if first is None or grid.width < 2:
        return False
    return all(cell is first for cell in grid.cells[r]) and first.colspan >= grid.width


def _has_colspan_origin(grid: Grid, r: int) -> bool:
    """Whether a cell spanning several columns starts on this row.

    A colspan is the mark of a group header. A rowspan alone is not: a row
    label spanning two data rows is common in tables with no header at all.
    """
    for c in range(grid.width):
        cell = grid.cells[r][c]
        if cell is not None and grid.origin[r][c] and cell.colspan > 1:
            return True
    return False


def _rowspan_reaches(grid: Grid, r: int) -> bool:
    """Whether a cell from a row above still covers row ``r``."""
    return any(
        cell is not None and not grid.origin[r][c] and r > 0 and grid.cells[r - 1][c] is cell
        for c, cell in enumerate(grid.cells[r])
    )


def _all_th(grid: Grid, r: int) -> bool:
    cells = [cell for cell in grid.cells[r] if cell is not None]
    return bool(cells) and all(cell.is_header for cell in cells)


def _looks_like_header_values(texts: list[str]) -> bool:
    """Whether a row's cells, the first aside, read as labels rather than data.

    A header row names its columns: years, words, phrases. A data row
    carries numbers. A row whose other cells are all empty is neither.
    """
    others = [t for t in texts[1:] if t]
    if not others:
        return False
    return all(_is_year(t) or not _is_number(t) for t in others)


def _all_years(texts: list[str]) -> bool:
    others = [t for t in texts[1:] if t]
    return bool(others) and all(_is_year(t) for t in others)


def detect_header_rows(grid: Grid) -> int:
    """How many leading rows are the header. Zero when none can be told.

    Structural signals first: rows inside ``<thead>``, rows made of ``<th>``.
    Without those, the first row is a header when it starts a span or its
    values read as labels rather than numbers; the header then grows
    downwards only for a structural reason — a cell from the header still
    spans into the row, the row above spans columns and this row holds the
    sub-labels, or the row is nothing but years. A row that is one cell
    across the whole table is a section heading and ends the header.
    """
    if grid.height == 0 or grid.width == 0:
        return 0

    count = 0
    while count < grid.height and grid.in_thead[count]:
        count += 1
    if count:
        return count

    while count < grid.height and _all_th(grid, count) and not _is_full_width_row(grid, count):
        count += 1
    if count:
        return count

    if _is_full_width_row(grid, 0):
        return 0
    if not (_has_colspan_origin(grid, 0) or _looks_like_header_values(_row_texts(grid, 0))):
        return 0
    count = 1

    while count < grid.height:
        if _is_full_width_row(grid, count):
            break
        if _rowspan_reaches(grid, count):
            # A header cell still covers this row: the row is its sub-labels.
            count += 1
            continue
        texts = _row_texts(grid, count)
        structural = _has_colspan_origin(grid, count - 1) or _all_years(texts)
        if structural and _looks_like_header_values(texts):
            count += 1
            continue
        break

    return count


def flatten_headers(grid: Grid, header_rows: int) -> list[str]:
    """One label per column from the header rows, levels joined with " / ".

    Down each column, every distinct cell — by identity, so a cell spanning
    several header rows counts once — contributes its text when it has one.
    An empty corner stays "".
    """
    labels: list[str] = []
    for c in range(grid.width):
        seen: list[Cell] = []
        parts: list[str] = []
        for r in range(header_rows):
            cell = grid.cells[r][c]
            if cell is None or any(cell is s for s in seen):
                continue
            seen.append(cell)
            if cell.text and (not parts or parts[-1] != cell.text):
                parts.append(cell.text)
        labels.append(HEADER_LEVEL_JOIN.join(parts))
    return labels


# ---- the body ----------------------------------------------------------------


def body_rows(grid: Grid, header_rows: int) -> list[list[str]]:
    """Each row below the header as a list of cell texts, one per column.

    A cell that spans columns shows its text in its leftmost column and
    leaves the rest of its span empty, so a value is never repeated as if it
    were several values. A cell that spans rows shows its text on each row
    it covers, so every row still says what it is about.
    """
    rows: list[list[str]] = []
    for r in range(header_rows, grid.height):
        line: list[str] = []
        for c in range(grid.width):
            cell = grid.cells[r][c]
            if cell is None:
                line.append("")
            elif c > 0 and grid.cells[r][c - 1] is cell:
                line.append("")  # covered by a colspan starting to the left
            else:
                line.append(cell.text)
        rows.append(line)
    return rows


# ---- rendering ---------------------------------------------------------------


def escape_cell(text: str) -> str:
    """A cell's text as one Markdown table cell: pipes escaped, no line breaks."""
    return text.replace("|", "\\|").replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def render_markdown_table(header: list[str], rows: list[list[str]]) -> str:
    width = max(len(header), *(len(r) for r in rows)) if rows else len(header)
    if width == 0:
        raise TableParseError("the table has no columns")

    def line(cells: list[str]) -> str:
        padded = list(cells) + [""] * (width - len(cells))
        return "| " + " | ".join(escape_cell(c) for c in padded) + " |"

    lines = [line(header), "| " + " | ".join("---" for _ in range(width)) + " |"]
    lines.extend(line(row) for row in rows)
    return "\n".join(lines)


def _fragments(value: Any) -> list[str]:
    """A caption or footnote list as clean strings, in order, empties out."""
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [f.strip() for f in value if isinstance(f, str) and f.strip()]


def format_caption(caption: Any) -> str | None:
    """The caption lines as one bold line, or None when there is no caption."""
    parts = _fragments(caption)
    if not parts:
        return None
    text = _WHITESPACE.sub(" ", CAPTION_JOIN.join(parts))
    return f"**{text}**"


def format_footnotes(footnotes: Any) -> str | None:
    """The footnotes as a heading and a list, in order, or None when none."""
    parts = _fragments(footnotes)
    if not parts:
        return None
    lines = [FOOTNOTES_HEADING, ""]
    lines.extend(f"- {_WHITESPACE.sub(' ', note)}" for note in parts)
    return "\n".join(lines)


@dataclass(frozen=True)
class ConvertedTable:
    """A table turned into Markdown, with what was found on the way."""

    markdown: str
    header_rows: int
    columns: int
    rows: int


def convert_html_table(
    table_html: str,
    caption: Any = None,
    footnotes: Any = None,
) -> ConvertedTable:
    """HTML in, Markdown out, with the counts a caller may want to report.

    Raises TableParseError when the HTML cannot be read safely.
    """
    rows = parse_html_table(table_html)
    grid = build_grid(rows)
    if grid.width == 0:
        raise TableParseError("the table has no cells")

    header_count = detect_header_rows(grid)
    header = flatten_headers(grid, header_count) if header_count else [""] * grid.width
    body = body_rows(grid, header_count)

    parts: list[str] = []
    caption_line = format_caption(caption)
    if caption_line:
        parts.append(caption_line)
    parts.append(render_markdown_table(header, body))
    notes = format_footnotes(footnotes)
    if notes:
        parts.append(notes)

    return ConvertedTable(
        markdown="\n\n".join(parts),
        header_rows=header_count,
        columns=grid.width,
        rows=len(body),
    )


def html_table_to_markdown(
    table_html: str,
    caption: list[str] | str | None = None,
    footnotes: list[str] | str | None = None,
) -> str:
    """The Markdown for one table: caption, table, footnotes.

    Raises TableParseError when the HTML cannot be read safely.
    """
    return convert_html_table(table_html, caption, footnotes).markdown
