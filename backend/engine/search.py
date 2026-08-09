"""
Generic search in the reconstructed text of a content unit.

The surgery engine knows nothing about dates: it replaces a range of characters
with another text. Dates are therefore only one *finder* among others. This
module provides the generic finders — literal and regular expression — and the
merge mechanism that keeps two rules from stepping on each other.

Two details that matter on text extracted from a PDF:

- Blanks are approximate. A "space" in the reconstructed text may be a real
  space glyph, or a mere advance obtained by kerning. Loose mode lets a space in
  the query match any run of blanks, line break included: that is what makes it
  possible to find a name broken at the end of a line.
- Apostrophes and dashes exist in several Unicode variants. Loose mode makes
  them interchangeable, otherwise a search for "l'article" fails on a document
  that writes "l’article".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

# Character families considered equivalent in loose mode.
_LOOSE_CLASSES = [
    "'‘’ʼ´`",                 # apostrophes
    "-‐‑‒–—−",      # dashes
    '"“”«»',                  # quotes
]

_DOLLAR_REF = re.compile(r"\$(\d{1,2})|\$\{(\w+)\}")


@dataclass
class Finder:
    """A "search → replace" rule."""

    id: str
    kind: str = "text"              # text | regex | dates
    query: str = ""
    replace: str = ""
    case_sensitive: bool = False
    whole_word: bool = False
    loose: bool = True              # tolerant about blanks and punctuation
    enabled: bool = True

    def compile(self) -> Optional["re.Pattern"]:
        if self.kind == "dates" or not self.query:
            return None
        flags = 0 if self.case_sensitive else re.IGNORECASE
        if self.kind == "regex":
            try:
                return re.compile(self.query, flags | re.UNICODE)
            except re.error:
                return None
        pattern = _literal_pattern(self.query, self.loose)
        if self.whole_word:
            pattern = r"(?<![\wÀ-ɏ])" + pattern + r"(?![\wÀ-ɏ])"
        try:
            return re.compile(pattern, flags | re.UNICODE)
        except re.error:
            return None

    def error(self) -> Optional[str]:
        if self.kind == "regex" and self.query:
            try:
                re.compile(self.query)
            except re.error as e:
                return "invalid regular expression: %s" % e
        return None


def _literal_pattern(query: str, loose: bool) -> str:
    """Turns a literal query into a pattern, with tolerances if asked for."""
    if not loose:
        return re.escape(query)

    out: List[str] = []
    i = 0
    n = len(query)
    while i < n:
        ch = query[i]
        if ch.isspace():
            # A blank in the query accepts any run of blanks, line break
            # included: the reconstructed text of a PDF breaks lines where the
            # document breaks them.
            j = i
            while j < n and query[j].isspace():
                j += 1
            out.append(r"[\s   ]+")
            i = j
            continue
        for klass in _LOOSE_CLASSES:
            if ch in klass:
                out.append("[" + re.escape(klass) + "]")
                break
        else:
            out.append(re.escape(ch))
        i += 1
    return "".join(out)


def _to_backrefs(template: str) -> str:
    """Accepts $1 and ${name} on top of Python's own \\1 syntax."""
    def sub(m):
        return "\\g<%s>" % (m.group(1) or m.group(2))
    return _DOLLAR_REF.sub(sub, template)


@dataclass
class TextMatch:
    finder_id: str
    start: int
    end: int
    raw: str
    target: str
    kind: str = "text"


def run_finder(finder: Finder, text: str, limit: int = 5000) -> List[TextMatch]:
    """Every match of a finder in a text."""
    rx = finder.compile()
    if rx is None:
        return []

    template = _to_backrefs(finder.replace) if finder.kind == "regex" else None
    out: List[TextMatch] = []

    for m in rx.finditer(text):
        if m.start() == m.end():
            continue          # empty pattern: we do not loop on it
        if finder.kind == "regex":
            try:
                target = m.expand(template)
            except (re.error, IndexError):
                target = finder.replace
        else:
            target = finder.replace
        out.append(TextMatch(finder_id=finder.id, start=m.start(), end=m.end(),
                             raw=m.group(0), target=target, kind=finder.kind))
        if len(out) >= limit:
            break
    return out


def merge(matches: Sequence[TextMatch]) -> List[TextMatch]:
    """
    Merges the matches of several finders on one same text.

    Two rules can aim at overlapping areas — "Jean Dupont" and "Dupont" for
    instance. The same codes cannot be rewritten twice: the longest one wins,
    and at equal length the one declared first.
    """
    ordered = sorted(matches, key=lambda m: (m.start, -(m.end - m.start)))
    out: List[TextMatch] = []
    last_end = -1
    for m in ordered:
        if m.start >= last_end:
            out.append(m)
            last_end = m.end
    return out
