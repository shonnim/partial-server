"""Rendering a canonical structured table as Markdown, deterministically.

The structured JSON a table is normalised into (see ``table_normalizer``) is
the record; the Markdown is derived from it here, in plain Python, so that the
same structure always yields the same text. The model is never asked for
Markdown — a second model call would be a second chance to drift from the
values, and the text stored beside the JSON has to agree with it exactly.

Layout, top to bottom:

    **caption**

    | <label column> | header … |
    | --- | --- |
    | section label |  |  |          (a section row: label, then empty cells)
    | row label | value | value |

    Footnotes:
    - marker text
    - text

    Source: …

Each part appears only when the structure carries it. A column header is the
column's label, prefixed by its group when it has one, so a two-level header
("전망치" over "2016") reads as one cell without losing either level.
"""

from typing import Any

# What goes between a column's group and its label in the header cell.
_GROUP_SEPARATOR = " / "


def _cell(value: Any) -> str:
    """One Markdown table cell. Pipes and line breaks would break the row."""
    if value is None:
        return ""
    text = str(value)
    return text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ").replace("|", "\\|").strip()


def _column_header(column: dict[str, Any]) -> str:
    label = _cell(column.get("label"))
    group = _cell(column.get("group"))
    return f"{group}{_GROUP_SEPARATOR}{label}" if group else label


def _has_label_column(rows: list[dict[str, Any]]) -> bool:
    """Whether any row carries a label or is a section — if so the table gets
    a leading label column; a table of bare values does not."""
    for row in rows:
        if row.get("type") == "section":
            return True
        if row.get("label") not in (None, ""):
            return True
    return False


def render_table_markdown(table: dict[str, Any]) -> str:
    """The Markdown for one validated structured table."""
    columns: list[dict[str, Any]] = list(table.get("columns") or [])
    rows: list[dict[str, Any]] = list(table.get("rows") or [])
    keys = [str(c.get("key")) for c in columns]

    lines: list[str] = []

    caption = table.get("caption")
    if caption:
        lines.append(f"**{_cell(caption)}**")
        lines.append("")

    label_column = _has_label_column(rows)

    header = ([""] if label_column else []) + [_column_header(c) for c in columns]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join("---" for _ in header) + " |")

    for row in rows:
        if row.get("type") == "section":
            cells = [_cell(row.get("label"))] + ["" for _ in keys]
        else:
            values = row.get("values") or {}
            cells = ([_cell(row.get("label"))] if label_column else []) + [
                _cell(values.get(key)) for key in keys
            ]
        lines.append("| " + " | ".join(cells) + " |")

    footnotes: list[dict[str, Any]] = list(table.get("footnotes") or [])
    if footnotes:
        lines.append("")
        lines.append("Footnotes:")
        for note in footnotes:
            marker = _cell(note.get("marker"))
            text = _cell(note.get("text"))
            lines.append(f"- {marker} {text}".rstrip() if marker else f"- {text}")

    source = table.get("source")
    if source:
        lines.append("")
        lines.append(f"Source: {_cell(source)}")

    return "\n".join(lines)
