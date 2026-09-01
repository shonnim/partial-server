"""The shared Jinja2 environment and the display filters templates rely on.

Formatting lives here rather than in templates so the rules are testable and
identical everywhere a value is shown.
"""

import posixpath
import uuid
from pathlib import Path

from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).parent / "templates"

_SIZE_UNITS = ("B", "KB", "MB", "GB", "TB")

# The left sidebar, in render order.
NAV_ITEMS = (
    {"key": "dashboard", "label": "Dashboard", "icon": "📊", "url": "/"},
    {"key": "documents", "label": "Documents", "icon": "📄", "url": "/documents"},
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


templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["short_id"] = short_id
templates.env.filters["folder_path"] = folder_path
templates.env.filters["filesize"] = filesize
templates.env.globals["nav_items"] = NAV_ITEMS
