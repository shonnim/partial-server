r"""Undoing the parser's markdown escaping of a block's text.

The parser writes each block's text as markdown, so a character markdown
would read as syntax comes back with a backslash in front of it: ``15\~64세``
for a range, ``\* 두바이유(달러/배럴)`` for an annotation line. The backslash
is the parser's, not the page's, and nothing downstream of this tool reads
the text as markdown, so it is taken off before the text is stored.

Two things it is *not* taken off:

* ``\(``, ``\)`` and ``\cdot``, ``\triangle`` — the parser also emits LaTeX,
  for formulas and for glyphs it could not letter, and a backslash there is
  part of the formula rather than an escape. Only the few punctuation marks
  in ``_ESCAPED`` are unescaped; a backslash before a bracket or a letter is
  left exactly as it came.
* Table bodies, which the parser gives as HTML and never escapes.

A line opening with ``\-`` is a case of its own. It is not a hyphen: it is
the bullet ∙ the page printed, which the parser read as a dash and then
escaped. The dash is put back to the bullet, so a stored line keeps the rank
the page gave it — this corpus indents ■, then ○, then ∙.
"""

import re

#: The bullet a line-opening escaped dash stands for.
BULLET = "\u2219"

# A dash opening a line — nothing but indentation before it, whitespace or
# the end of the line after it. A "\-" inside a sentence is a real hyphen and
# is left to _ESCAPED to unescape.
_LEADING_DASH = re.compile(r"^([^\S\n]*)\\-(?=\s|$)", re.MULTILINE)

# The markdown punctuation the parser escapes. Deliberately short: every
# character in it is one LaTeX has no use for.
_ESCAPED = re.compile(r"\\([*\-~_#])")


def unescape(text: str) -> str:
    """One block's text as the page printed it, without markdown escaping."""
    return _ESCAPED.sub(r"\1", _LEADING_DASH.sub("\\1" + BULLET, text))
