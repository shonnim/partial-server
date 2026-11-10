"""The chunking strategy for group ``oft-1``: outline levels, ``○`` bullets.

The group's documents run to ~70 pages at most and share one front matter:
cover, then the table of contents on viewer page 3 (its letter-spaced
"목 차" title parses as one block per syllable), then the summary page,
titled "요 약" the same way, then the body.

Inside a section the text is layered like this:

    plain text                     opening prose
    □ text                        — but the square never reaches the parsed
                                    JSON, so these lines arrive as plain text
    ○ item                        the top *visible* bullet
      ∙ sub-item                  often printed as "-"

Chunking is ``ImfHierarchicalChunker``'s walk with two differences:

* **Levels come from the stored outline.** The group's headings carry no
  I./1./가. numbering to detect, so ``LEVEL_PATTERNS`` is empty and every
  header's depth is its ``level`` in the ``headers`` table — which the TOC
  extraction already settled. Nothing is warned, and no plain-text row can
  move the breadcrumb (there is no marker to recognise one by).
* **Text chunks prefer to be cut at ``○`` lines**, so a bullet keeps its
  ``∙``/``-`` children. The printed ``□`` level would be the better cut, but
  the parser drops the square, so those lines cannot be told from prose.

One repair of the group's parses on top: a text row whose content is a single
character is a split title's syllable (the summary page's "요" and "약"), not
content, and is dropped before chunking.

Everything else (footnote folding, table joining, budgets, ordering) is
inherited; see ``chunking_imf1`` for the full picture.
"""

import re
from collections.abc import Mapping, Sequence

from app.models.header import Header
from app.services.chunking_imf1 import ImfHierarchicalChunker, _Row, _squash


class OftHierarchicalChunker(ImfHierarchicalChunker):
    """The ``oft-1`` strategy. See the module docstring."""

    # No numbering markers: every level is read from the stored outline.
    LEVEL_PATTERNS: Sequence[tuple[int, re.Pattern[str]]] = ()
    # The top visible bullet, where a text chunk prefers to be cut. The
    # parser prints the circle as ○, ◯ or 〇.
    BOX_BULLET = re.compile(r"^\s*[○◯〇]")

    @staticmethod
    def _filter(rows: list[_Row], headers: Mapping[int, Header]) -> list[_Row]:
        """The base filter, then the split-title syllables.

        A letter-spaced page title ("요 약") parses as one single-character
        block per syllable; filed under their header, those blocks would ride
        into its first chunk as stray characters.
        """
        kept = ImfHierarchicalChunker._filter(rows, headers)
        return [
            row
            for row in kept
            if not (row.type == "text" and len(_squash(row.content)) <= 1)
        ]
