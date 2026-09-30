"""Turning a parsed table block into canonical structured JSON.

The parser hands a table over as HTML (``table_body``) with the caption and
footnotes beside it. HTML says where the cells sit, not what they mean: a
two-row header with ``colspan`` is a hierarchy, a row of one merged cell is a
section, a trailing "출처:" line is the source. That reading is left to the
model — the one call this module makes — and everything the model is *not*
trusted with is done here:

* the reply is validated against the source before it is kept: every cell
  the HTML held has to be present in the structure, the caption and each
  footnote have to survive, and every row value has to name a real column;
* a reply that fails is sent back with the reasons, a bounded number of
  times, rather than stored;
* the Markdown is never the model's — ``table_markdown`` renders it from the
  validated structure.

Nothing is repaired quietly. A table whose reply cannot be validated within
the retry budget raises ``TableNormalizationError`` with the last reasons,
and the caller decides what a failed table means for the document.
"""

import json
import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

from app.config import get_settings
from app.llm import LLMError, chat_json
from app.services.parser_text import unescape
from app.services.table_markdown import render_table_markdown

# How many times the model is asked before the table is given up on. The
# first attempt is the plain request; each one after carries the validation
# failures of the one before.
MAX_ATTEMPTS = 3

# A table's HTML is the bulk of the prompt. Beyond this the model's context is
# the risk, and a table that long is one to look at by hand anyway.
MAX_TABLE_BODY_CHARS = 60_000

# The structured reply can run to several thousand tokens for a wide table.
MAX_REPLY_TOKENS = 12_000

# What the parser gives a table that has no caption or footnote: an empty
# list. Fragments are joined with a line break so a caption the parser split
# over two lines is stored as it was printed, nothing dropped and nothing
# run together.
_FRAGMENT_JOIN = "\n"


class TableNormalizationError(RuntimeError):
    """The table could not be turned into a structure worth keeping."""


# ---- the shape the model is held to ------------------------------------------

_NULLABLE_STRING = {"type": ["string", "null"]}

TABLE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["table"]},
        "caption": _NULLABLE_STRING,
        "source": _NULLABLE_STRING,
        "columns": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "label": {"type": "string"},
                    "group": _NULLABLE_STRING,
                    "year": {"type": ["integer", "null"]},
                },
                "required": ["key", "label", "group", "year"],
                "additionalProperties": False,
            },
        },
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["data", "section"]},
                    "section": _NULLABLE_STRING,
                    "label": _NULLABLE_STRING,
                    "values": {
                        "type": "object",
                        "additionalProperties": _NULLABLE_STRING,
                    },
                },
                "required": ["type", "section", "label", "values"],
                "additionalProperties": False,
            },
        },
        "footnotes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "marker": _NULLABLE_STRING,
                    "text": {"type": "string"},
                },
                "required": ["marker", "text"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["type", "caption", "source", "columns", "rows", "footnotes"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You reconstruct the logical structure of a table that was extracted from a PDF.

You are given the table as HTML, plus its caption and its footnotes as the
parser read them off the page. Return one JSON object describing the table's
logical structure — the columns, the rows, and the values — independent of
the HTML layout.

Reconstruct the structure; do not transliterate the HTML:

1. Resolve every rowspan and colspan. A header cell spanning several columns
is the group of each of those columns. A header cell spanning several rows
is one header, not several.
2. Rebuild multi-level and hierarchical headers. Each output column has a
"label" (the lowest header cell above it, exactly as written) and a "group"
(the header cell above that, exactly as written, or null when there is
none). Deeper hierarchies are flattened into the group by joining the levels
with " / ". "year" is the year the column is about when its label is a
year, otherwise null. "key" is a short snake_case ASCII identifier you
choose, unique per column; keys are not shown to anyone.
3. The leftmost column is usually row labels, not values. When it is, put
its text in each row's "label" and do not list that column in "columns".
Row labels are copied exactly, including footnote markers such as "1)".
4. A row whose only content is a heading (one merged cell, or a label with
every value empty) is a section row: {"type": "section", "label": ...}.
Data rows that follow it carry that heading in "section" until the next
section row. Rows that carry values are data rows: {"type": "data", ...}.
5. "values" maps every column key to the cell text for that row. Copy each
value exactly as written: numbers, signs, decimal places, percentages,
dates, units, currency symbols, footnote markers, parentheses, dashes. Do
not compute, round, convert, translate or tidy anything. Do not correct what
looks like a mistake.
6. A cell that is empty stays empty: null. Never write 0, "-", "N/A" or any
other stand-in for a value that is not in the source.
7. Never add a row, a column or a value that is not in the source, and never
drop one. Every cell of the HTML must be accounted for somewhere in the
output — as a column label or group, a row label, a section label, or a value.
8. "caption" is the caption exactly as given, or null when none was given.
9. "footnotes" lists every footnote given, in the order given, each as
{"marker", "text"}. The marker is the leading "1)", "2)", "*", "주:" and the
like when the note begins with one; "text" is the rest, exactly. A note with
no marker has marker null. Keep the text exact, including the markers that
refer to cells.
10. A footnote that names where the table comes from ("출처:", "자료:",
"Source:") is the "source": copy the text after that word into "source" and
also keep the note in "footnotes". When there is no such note, "source" is
null.
11. Any table shape is possible: one column or fifty, no header at all, headers
three levels deep, numeric or text, with or without row labels. Describe
what is there.

Return only the JSON object. No Markdown, no code fence, no explanation."""


# ---- reading the source ------------------------------------------------------


class _CellCollector(HTMLParser):
    """Every td/th text in document order, entities decoded."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cells: list[str] = []
        self._parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("td", "th"):
            self._parts = []
        elif tag == "br" and self._parts is not None:
            self._parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._parts is not None:
            self.cells.append("".join(self._parts))
            self._parts = None

    def handle_data(self, data: str) -> None:
        if self._parts is not None:
            self._parts.append(data)


def html_cells(table_body: str) -> list[str]:
    """The text of every cell in the HTML, in reading order, empties included."""
    collector = _CellCollector()
    collector.feed(table_body)
    collector.close()
    return collector.cells


_WHITESPACE = re.compile(r"\s+")


def squash(text: str) -> str:
    """Text reduced to what a copy must keep: no whitespace, NFKC-folded.

    Spacing is the one thing the model is allowed to change — "1 ) " and
    "1)" are the same footnote marker — so it is taken out of both sides of
    every comparison.
    """
    return _WHITESPACE.sub("", unicodedata.normalize("NFKC", text))


def join_fragments(fragments: Any) -> str | None:
    """A caption or footnote list as one string, or None when there is none.

    The parser writes these as lists of strings; anything else is taken as
    absent rather than guessed at. Each part comes off the parser's markdown
    escaping (see ``parser_text``), so a caption reads as the page printed it.
    """
    if isinstance(fragments, str):
        text = unescape(fragments).strip()
        return text or None
    if not isinstance(fragments, list):
        return None
    parts = _fragment_list(fragments)
    return _FRAGMENT_JOIN.join(parts) if parts else None


def _fragment_list(fragments: Any) -> list[str]:
    if isinstance(fragments, str):
        fragments = [fragments]
    if not isinstance(fragments, list):
        return []
    kept = (unescape(f).strip() for f in fragments if isinstance(f, str))
    return [f for f in kept if f]


@dataclass(frozen=True)
class TableSource:
    """What the parser gave for one table block."""

    body: str
    caption: str | None
    footnotes: list[str]

    @classmethod
    def from_block(cls, block: dict[str, Any]) -> "TableSource":
        body = block.get("table_body")
        if not isinstance(body, str) or not body.strip():
            raise TableNormalizationError("the block has no table_body")
        if len(body) > MAX_TABLE_BODY_CHARS:
            raise TableNormalizationError(
                f"table_body is {len(body):,} characters, above the "
                f"{MAX_TABLE_BODY_CHARS:,} limit"
            )
        return cls(
            body=body,
            caption=join_fragments(block.get("table_caption")),
            footnotes=_fragment_list(block.get("table_footnote")),
        )


def build_user_prompt(source: TableSource, feedback: list[str] | None = None) -> str:
    parts = []
    if feedback:
        parts.append(
            "Your previous answer was rejected for these reasons. Fix every one "
            "of them and return the whole table again:\n"
            + "\n".join(f"- {reason}" for reason in feedback)
        )
    parts.append(f"Caption:\n{source.caption or '(none)'}")
    notes = "\n".join(source.footnotes) if source.footnotes else "(none)"
    parts.append(f"Footnotes, in order:\n{notes}")
    parts.append(f"Table HTML:\n{source.body}")
    return "\n\n".join(parts)


# ---- holding the reply to the source -----------------------------------------


def _strings_in(value: Any) -> list[str]:
    """Every string anywhere inside a JSON value."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings_in(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings_in(v)]
    return []


def validate_structured(table: Any, source: TableSource) -> list[str]:
    """Why ``table`` cannot be kept, as a list of reasons — empty when it can.

    The checks are the ones a person would make with the PDF beside them:
    does every cell still exist, is every footnote still there, does each row
    point at a column that exists.
    """
    problems: list[str] = []

    if not isinstance(table, dict):
        return ["the reply is not a JSON object"]
    if table.get("type") != "table":
        problems.append('"type" must be "table"')

    columns = table.get("columns")
    if not isinstance(columns, list):
        problems.append('"columns" must be an array')
        columns = []
    keys: list[str] = []
    for i, column in enumerate(columns):
        if not isinstance(column, dict):
            problems.append(f"column {i} is not an object")
            continue
        key = column.get("key")
        if not isinstance(key, str) or not key.strip():
            problems.append(f"column {i} has no key")
            continue
        if key in keys:
            problems.append(f'column key "{key}" is used more than once')
        keys.append(key)
        if not isinstance(column.get("label"), str):
            problems.append(f'column "{key}" has no label')

    rows = table.get("rows")
    if not isinstance(rows, list):
        problems.append('"rows" must be an array')
        rows = []
    known = set(keys)
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            problems.append(f"row {i} is not an object")
            continue
        kind = row.get("type")
        if kind == "section":
            if not isinstance(row.get("label"), str) or not row["label"].strip():
                problems.append(f"section row {i} has no label")
            continue
        if kind != "data":
            problems.append(f'row {i} has type {kind!r}; expected "data" or "section"')
            continue
        values = row.get("values")
        if not isinstance(values, dict):
            problems.append(f'data row {i} has no "values" object')
            continue
        unknown = sorted(k for k in values if k not in known)
        if unknown:
            problems.append(
                f"data row {i} has values for columns that do not exist: "
                + ", ".join(f'"{k}"' for k in unknown)
            )
        for key, value in values.items():
            if value is not None and not isinstance(value, str):
                problems.append(f'data row {i}, column "{key}": value must be a string or null')

    footnotes = table.get("footnotes")
    if not isinstance(footnotes, list):
        problems.append('"footnotes" must be an array')
        footnotes = []

    # Everything the model wrote, whitespace-free, is the haystack every
    # source string has to be found in.
    haystack = squash("\n".join(_strings_in(table)))

    if source.caption:
        if not isinstance(table.get("caption"), str) or not table["caption"].strip():
            problems.append("the caption was dropped")
        elif squash(source.caption) not in haystack:
            problems.append(f"the caption was changed; it must read exactly: {source.caption!r}")

    missing: list[str] = []
    seen: set[str] = set()
    for cell in html_cells(source.body):
        needle = squash(cell)
        if not needle or needle in seen:
            continue
        seen.add(needle)
        if needle not in haystack:
            missing.append(cell.strip())
    if missing:
        shown = ", ".join(repr(m) for m in missing[:12])
        more = f" (and {len(missing) - 12} more)" if len(missing) > 12 else ""
        problems.append(
            f"{len(missing)} cell value(s) from the HTML are missing from the "
            f"output and must be kept exactly: {shown}{more}"
        )

    # Footnotes: each one given has to come back, in order, either as a
    # footnote or as the source line. Containment either way, whitespace
    # aside, because the model splits the marker off the text.
    candidates: list[str] = []
    for note in footnotes:
        if isinstance(note, dict):
            marker = note.get("marker") if isinstance(note.get("marker"), str) else ""
            text = note.get("text") if isinstance(note.get("text"), str) else ""
            candidates.append(squash(marker + text))
    if isinstance(table.get("source"), str):
        candidates.append(squash(table["source"]))

    position = 0
    for original in source.footnotes:
        wanted = squash(original)
        if not wanted:
            continue
        found = next(
            (
                i
                for i in range(position, len(candidates))
                if candidates[i] and (wanted in candidates[i] or candidates[i] in wanted)
            ),
            None,
        )
        if found is None:
            problems.append(f"footnote was dropped or changed; keep it exactly: {original!r}")
        else:
            position = found if found < len(footnotes) else position

    return problems


# ---- the call -----------------------------------------------------------------


@dataclass(frozen=True)
class NormalizedTable:
    """A table the model reconstructed and the checks accepted."""

    structured: dict[str, Any]
    markdown: str
    caption: str | None
    attempts: int


def normalize_table(block: dict[str, Any]) -> NormalizedTable:
    """Reconstruct one parsed table block, validate it, and render it.

    Raises TableNormalizationError when the block has no usable body, when
    the model cannot be reached, or when no attempt survives validation.
    """
    source = TableSource.from_block(block)

    feedback: list[str] | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            reply = chat_json(
                system=SYSTEM_PROMPT,
                user=build_user_prompt(source, feedback),
                schema=TABLE_SCHEMA,
                schema_name="structured_table",
                max_tokens=MAX_REPLY_TOKENS,
                timeout_seconds=get_settings().llm_table_timeout_seconds,
            )
        except LLMError as exc:
            raise TableNormalizationError(f"attempt {attempt}: {exc}") from exc

        problems = validate_structured(reply, source)
        if not problems:
            return NormalizedTable(
                structured=reply,
                markdown=render_table_markdown(reply),
                caption=source.caption,
                attempts=attempt,
            )
        feedback = problems

    assert feedback is not None
    raise TableNormalizationError(
        f"rejected after {MAX_ATTEMPTS} attempts: " + "; ".join(feedback)
    )


def dump_structured(table: dict[str, Any]) -> str:
    """The structure as text, for logs and the review pop-up."""
    return json.dumps(table, ensure_ascii=False, indent=2)
