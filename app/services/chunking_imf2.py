"""The chunking strategy for group ``imf-2``: ``imf-1`` with other markers.

The documents are laid out like the ``imf-1`` group with two differences in
the markers, and nothing else:

    I    Part            (roman numeral, dot optional) ─┐
    1.   Chapter         (arabic numeral)               ├─ chunk boundaries and breadcrumb
    가.  Section         (hangul, often absent)        ─┘
    ○ ■ -                body bullets, three levels deep — content, never a boundary

So the part heading is "Ⅰ 서론" or "I. 세계 경제전망과 정책" — the group's
documents print it both ways — and the top body bullet — where a text chunk
prefers to be cut, so a bullet keeps its children — is ``○`` rather than
``□``. Everything else (footnote folding,
table joining, the walk, the budgets) is ``ImfHierarchicalChunker``'s; see
``chunking_imf1`` for the full picture.
"""

import re

from app.services.chunking_imf1 import (
    _LEVEL_2,
    _LEVEL_3,
    _ROMAN,
    ImfHierarchicalChunker,
)

# "Ⅰ 서론", "II 재정 전망", and the group's documents also print the dotted
# imf-1 form, "I. 세계 경제전망과 정책". The dotted branch is imf-1's rule —
# the lookahead keeps "I.V." and "I.1" out, Hangul right after the dot is
# fine — and the bare branch keeps English prose ("I think …") out; a title
# opens with Hangul or a capital.
_LEVEL_1 = re.compile(rf"^\s*{_ROMAN}(?:\s*\.(?![A-Za-z0-9)])|\s+(?![a-z]))")


class Imf2HierarchicalChunker(ImfHierarchicalChunker):
    """The ``imf-2`` strategy. See the module docstring."""

    LEVEL_PATTERNS = ((1, _LEVEL_1), (2, _LEVEL_2), (3, _LEVEL_3))
    # The parser prints the circle as ○, ◯ or 〇.
    BOX_BULLET = re.compile(r"^\s*[○◯〇]")
