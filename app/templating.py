"""The shared Jinja2 environment and the display filters templates rely on.

Formatting lives here rather than in templates so the rules are testable and
identical everywhere a value is shown.
"""

import posixpath
import uuid
from pathlib import Path

from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

_SIZE_UNITS = ("B", "KB", "MB", "GB", "TB")

# The left sidebar, in render order.
NAV_ITEMS = (
    {"key": "dashboard", "label": "Dashboard", "icon": "📊", "url": "/"},
    {"key": "documents", "label": "Documents", "icon": "📄", "url": "/documents"},
    {"key": "parsing", "label": "Parsing", "icon": "⚙️", "url": "/parsing"},
    {"key": "headers", "label": "Headers", "icon": "🗂️", "url": "/headers"},
    {"key": "sections", "label": "Section", "icon": "📑", "url": "/sections"},
    {"key": "grouping", "label": "Grouping", "icon": "🏷️", "url": "/grouping"},
    {"key": "chunking", "label": "Chunking", "icon": "🧩", "url": "/chunking"},
)


def short_id(value: uuid.UUID | str) -> str:
    """Last 4 characters of an id — enough to tell rows apart at a glance."""
    return str(value)[-4:]


def folder_path(s3_key: str) -> str:
    """The directory part of an object key, without the object itself."""
    return posixpath.dirname(s3_key) or "/"


def filesize(num_bytes: int | None) -> str:
    if num_bytes is None:
        return "—"
    size = float(num_bytes)
    for unit in _SIZE_UNITS:
        if size < 1024 or unit == _SIZE_UNITS[-1]:
            # Bytes are whole; larger units read better with one decimal.
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def static_url(path: str) -> str:
    """The URL of a static file, stamped with its modification time.

    The stamp changes whenever the file does, so a browser that cached the
    previous script or stylesheet fetches the new one on the next page load
    instead of running stale code against a fresh template.
    """
    file = STATIC_DIR / path
    version = int(file.stat().st_mtime) if file.exists() else 0
    return f"/static/{path}?v={version}"


templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["short_id"] = short_id
templates.env.filters["folder_path"] = folder_path
templates.env.filters["filesize"] = filesize
templates.env.globals["nav_items"] = NAV_ITEMS
templates.env.globals["static_url"] = static_url
