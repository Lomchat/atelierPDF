"""
Date detection and rewriting, with strict preservation of the source format.

Two non-negotiable requirements:

1. **Do not get the format wrong.** "03/04/2024" means 3 April or 4 March
   depending on who produced the document. The ambiguity is settled by looking
   at the *whole* document: if it contains a "25/12/2024", we know the
   convention is day-month, and we apply it to the ambiguous dates. Lacking
   proof, we mark the match as ambiguous and the interface asks the human.

2. **Render the output date in exactly the same mould.** If the source writes
   "07 mars 2024", the output must be "12 avril 2025" and not "12/04/2025":
   same separator, same zero padding, same case, same language, same
   abbreviation, same "1er".
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# --------------------------------------------------------------------------
# Month names
# --------------------------------------------------------------------------

MONTHS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
             "août", "septembre", "octobre", "novembre", "décembre"]
MONTHS_FR_ABBR = ["janv", "févr", "mars", "avr", "mai", "juin", "juil",
                  "août", "sept", "oct", "nov", "déc"]
MONTHS_EN = ["january", "february", "march", "april", "may", "june", "july",
             "august", "september", "october", "november", "december"]
MONTHS_EN_ABBR = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug",
                  "sep", "oct", "nov", "dec"]


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def _build_month_lookup() -> Dict[str, Tuple[int, str, bool]]:
    """normalised key -> (month 1-12, language, abbreviated?)"""
    out: Dict[str, Tuple[int, str, bool]] = {}
    for i, n in enumerate(MONTHS_FR):
        out[_strip_accents(n)] = (i + 1, "fr", False)
    for i, n in enumerate(MONTHS_FR_ABBR):
        out.setdefault(_strip_accents(n), (i + 1, "fr", True))
    for i, n in enumerate(MONTHS_EN):
        out.setdefault(_strip_accents(n), (i + 1, "en", False))
    for i, n in enumerate(MONTHS_EN_ABBR):
        out.setdefault(_strip_accents(n), (i + 1, "en", True))
    out.setdefault("sept", (9, "en", True))
    return out


MONTH_LOOKUP = _build_month_lookup()

_ALL_MONTH_WORDS = sorted(
    set(MONTHS_FR + MONTHS_FR_ABBR + MONTHS_EN + MONTHS_EN_ABBR)
    | {_strip_accents(w) for w in MONTHS_FR + MONTHS_FR_ABBR}
    | {"sept"},
    key=len, reverse=True,
)
_MONTH_ALT = "|".join(re.escape(w) for w in _ALL_MONTH_WORDS)

# --------------------------------------------------------------------------
# Patterns
# --------------------------------------------------------------------------

# 15/03/2024 · 15-03-24 · 15.03.2024 · 2024-03-15 · 2024/03/15
# The lookahead must forbid only a separator *followed by a digit*:
# "30/04/2024." at the end of a sentence must match, "1.2.3.4" must not.
RE_NUMERIC = re.compile(
    r"(?<![\d])(?<![\d][/.\-])"
    r"(?P<a>\d{1,4})(?P<s1>[/.\-])(?P<b>\d{1,2})(?P<s2>[/.\-])(?P<c>\d{2,4})"
    r"(?!\d)(?![/.\-]\d)"
)

# 15 mars 2024 · 1er mars 2024 · 15 mars · mars 2024 · March 15, 2024
RE_TEXTUAL = re.compile(
    r"(?<![\wÀ-ÿ])"
    r"(?:"
    r"(?P<d1>\d{1,2})(?P<sfx>er|ème|e|st|nd|rd|th)?[\s ]*"
    r"(?P<m1>" + _MONTH_ALT + r")\.?"
    r"(?:[\s ]*(?P<y1>\d{4}|\d{2}(?![\d])))?"
    r"|"
    r"(?P<m2>" + _MONTH_ALT + r")\.?[\s ]*"
    r"(?:(?P<d2>\d{1,2})(?P<sfx2>er|st|nd|rd|th)?[\s ]*,?[\s ]*)?"
    r"(?P<y2>\d{4})"
    r")"
    r"(?![\wÀ-ÿ])",
    re.IGNORECASE,
)

RE_YEAR_ONLY = re.compile(
    r"(?<![\d])(?<![\d][/.\-])(?P<y>(?:19|20)\d{2})(?!\d)(?![/.\-]\d)")


# --------------------------------------------------------------------------
# Format
# --------------------------------------------------------------------------


@dataclass
class DateFormat:
    """The exact mould in which the date was written."""

    kind: str = "numeric"          # numeric | textual | year
    order: str = "DMY"             # DMY | MDY | YMD | MY | DM | Y
    sep1: str = "/"
    sep2: str = "/"
    day_pad: int = 2               # 1 = no leading zero, 2 = with
    month_pad: int = 2
    year_digits: int = 4
    month_style: str = "numeric"   # numeric | full | abbr
    month_case: str = "lower"      # lower | title | upper
    month_lang: str = "fr"
    month_dot: bool = False        # "janv." with the dot
    day_suffix: Optional[str] = None   # the "er" of "1er"
    comma: bool = False            # "March 15, 2024"
    space: str = " "               # space used (sometimes non-breaking)
    # If the source writes "decembre" without the accent, the output must write
    # "fevrier" without the accent: otherwise we introduce an "é" that may well
    # not exist in the font subset, and above all we betray the style of the
    # document.
    month_accents: bool = True

    def _month_word(self, month: int) -> str:
        table = (MONTHS_FR_ABBR if self.month_style == "abbr" else MONTHS_FR) \
            if self.month_lang == "fr" else \
            (MONTHS_EN_ABBR if self.month_style == "abbr" else MONTHS_EN)
        w = table[month - 1]
        if not self.month_accents:
            w = _strip_accents(w)
        if self.month_case == "title":
            w = w[:1].upper() + w[1:]
        elif self.month_case == "upper":
            w = w.upper()
        if self.month_style == "abbr" and self.month_dot and w.lower() not in ("mars", "mai", "juin", "août", "aout"):
            w += "."
        return w

    def render(self, d: Optional[int], m: Optional[int], y: Optional[int]) -> str:
        if self.kind == "year":
            return self._year(y)

        if self.kind == "numeric":
            ds = str(d).zfill(self.day_pad) if d is not None else ""
            ms = str(m).zfill(self.month_pad) if m is not None else ""
            ys = self._year(y)
            if self.order == "YMD":
                return "%s%s%s%s%s" % (ys, self.sep1, ms, self.sep2, ds)
            if self.order == "MDY":
                return "%s%s%s%s%s" % (ms, self.sep1, ds, self.sep2, ys)
            if self.order == "MY":
                return "%s%s%s" % (ms, self.sep1, ys)
            return "%s%s%s%s%s" % (ds, self.sep1, ms, self.sep2, ys)

        # textual
        mw = self._month_word(m) if m else ""
        ys = self._year(y) if y is not None else ""
        if self.order == "MY":
            return "%s%s%s" % (mw, self.space, ys) if ys else mw
        if self.order == "MDY":
            ds = str(d).zfill(self.day_pad) if d is not None else ""
            if self.day_suffix:
                ds += self._suffix(d)
            head = "%s%s%s" % (mw, self.space, ds)
            if not ys:
                return head
            return "%s%s%s%s" % (head, "," if self.comma else "", self.space, ys)
        # DMY / DM
        ds = str(d).zfill(self.day_pad) if d is not None else ""
        if self.day_suffix:
            ds += self._suffix(d)
        out = "%s%s%s" % (ds, self.space, mw)
        if ys:
            out += "%s%s" % (self.space, ys)
        return out

    def _suffix(self, d: Optional[int]) -> str:
        if self.month_lang == "fr":
            return "er" if d == 1 else ""
        if d is None:
            return ""
        if d in (11, 12, 13):
            return "th"
        return {1: "st", 2: "nd", 3: "rd"}.get(d % 10, "th")

    def _year(self, y: Optional[int]) -> str:
        if y is None:
            return ""
        return str(y % 100).zfill(2) if self.year_digits == 2 else str(y)

    def describe(self) -> str:
        if self.kind == "year":
            return "year only"
        if self.kind == "numeric":
            sample = self.render(1, 2, 2024)
            return "numeric %s (%s)" % (self.order, sample)
        return "spelled-out %s %s (%s)" % (
            self.order, self.month_lang, self.render(1, 2, 2024))


# --------------------------------------------------------------------------
# Detected match
# --------------------------------------------------------------------------


@dataclass
class DateMatch:
    start: int
    end: int
    raw: str
    day: Optional[int]
    month: Optional[int]
    year: Optional[int]
    fmt: DateFormat
    ambiguous: bool = False
    alt: Optional[Tuple[Optional[int], Optional[int], Optional[int]]] = None
    partial: bool = False          # incomplete date (month/year only)
    confidence: float = 1.0

    @property
    def iso(self) -> Optional[str]:
        if self.year and self.month and self.day:
            try:
                return dt.date(self.year, self.month, self.day).isoformat()
            except ValueError:
                return None
        return None

    @property
    def key(self) -> str:
        """
        Grouping key for the matches of one same date, whatever the writing
        format. "15/03/2024", "15 mars 2024" and "2024-03-15" share the same
        key: that is what lets you say "every date A becomes B" in a single
        rule.
        """
        if self.year and self.month and self.day:
            return "%04d-%02d-%02d" % (self.year, self.month, self.day)
        if self.year and self.month:
            return "%04d-%02d" % (self.year, self.month)
        if self.year:
            return "%04d" % self.year
        return "?"

    def as_date(self) -> Optional[dt.date]:
        if self.year and self.month and self.day:
            try:
                return dt.date(self.year, self.month, self.day)
            except ValueError:
                return None
        return None


def _expand_year(y: int, digits: int, pivot: int = 68) -> int:
    if digits >= 4 or y > 99:
        return y
    return 2000 + y if y <= pivot else 1900 + y


def _valid_dmy(d: int, m: int, y: int) -> bool:
    if not (1 <= m <= 12 and 1 <= d <= 31 and 1 <= y <= 9999):
        return False
    try:
        dt.date(y, m, d)
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------


def _scan_numeric(text: str) -> List[DateMatch]:
    out: List[DateMatch] = []
    for m in RE_NUMERIC.finditer(text):
        a_s, b_s, c_s = m.group("a"), m.group("b"), m.group("c")
        s1, s2 = m.group("s1"), m.group("s2")
        if s1 != s2:
            continue
        a, b, c = int(a_s), int(b_s), int(c_s)

        candidates: List[Tuple[str, int, int, int]] = []
        # YYYY-MM-DD
        if len(a_s) == 4:
            if _valid_dmy(c, b, a):
                candidates.append(("YMD", c, b, a))
        else:
            y = _expand_year(c, len(c_s))
            if _valid_dmy(a, b, y):
                candidates.append(("DMY", a, b, y))
            if _valid_dmy(b, a, y):
                candidates.append(("MDY", b, a, y))
        if not candidates:
            continue

        order, d, mo, y = candidates[0]
        fmt = DateFormat(
            kind="numeric", order=order, sep1=s1, sep2=s2,
            day_pad=2 if (len(a_s if order != "MDY" else b_s) == 2) else 1,
            month_pad=2 if (len(b_s if order != "MDY" else a_s) == 2) else 1,
            year_digits=4 if len(c_s) == 4 or len(a_s) == 4 else 2,
        )
        if order == "YMD":
            fmt.day_pad = 2 if len(c_s) == 2 else 1
            fmt.month_pad = 2 if len(b_s) == 2 else 1
            fmt.year_digits = 4

        dm = DateMatch(m.start(), m.end(), m.group(0), d, mo, y, fmt)
        if len(candidates) > 1:
            dm.ambiguous = True
            dm.alt = (candidates[1][1], candidates[1][2], candidates[1][3])
        out.append(dm)
    return out


def _month_from_word(word: str) -> Optional[Tuple[int, str, bool]]:
    return MONTH_LOOKUP.get(_strip_accents(word.lower().rstrip(".")))


def _case_of(word: str) -> str:
    if word.isupper() and len(word) > 1:
        return "upper"
    if word[:1].isupper():
        return "title"
    return "lower"


def _uses_accents(word: str, month: int, lang: str, abbr: bool) -> bool:
    """Does the document accent its month names?

    "décembre" -> yes. "decembre" -> no. "mars" -> impossible to tell, we keep
    the accented behaviour by default.
    """
    if word != _strip_accents(word):
        return True
    table = (MONTHS_FR_ABBR if abbr else MONTHS_FR) if lang == "fr" \
        else (MONTHS_EN_ABBR if abbr else MONTHS_EN)
    canonical = table[month - 1]
    return canonical == _strip_accents(canonical)


def _scan_textual(text: str) -> List[DateMatch]:
    out: List[DateMatch] = []
    for m in RE_TEXTUAL.finditer(text):
        raw = m.group(0)
        mw = m.group("m1") or m.group("m2")
        if not mw:
            continue
        info = _month_from_word(mw)
        if not info:
            continue
        month, lang, abbr = info

        d_s = m.group("d1") or m.group("d2")
        y_s = m.group("y1") or m.group("y2")
        sfx = m.group("sfx") or m.group("sfx2")

        day = int(d_s) if d_s else None
        if day is not None and not 1 <= day <= 31:
            continue
        year = None
        year_digits = 4
        if y_s:
            year_digits = len(y_s)
            year = _expand_year(int(y_s), year_digits)
        if day and year and not _valid_dmy(day, month, year):
            continue

        order = "DMY" if m.group("m1") else ("MDY" if d_s else "MY")
        if order == "DMY" and not year:
            order = "DM"

        # space actually used (sometimes non-breaking)
        sp = " "
        sp_match = re.search(r"[\s ]", raw)
        if sp_match:
            sp = sp_match.group(0)
            if sp in "\r\n\t":
                sp = " "

        fmt = DateFormat(
            kind="textual", order=order,
            day_pad=2 if (d_s and len(d_s) == 2 and d_s[0] == "0") else 1,
            year_digits=year_digits if year_digits in (2, 4) else 4,
            month_style="abbr" if abbr else "full",
            month_case=_case_of(mw),
            month_lang=lang,
            month_dot=("." in raw[raw.lower().find(mw.lower()):][:len(mw) + 1]),
            day_suffix=sfx,
            comma="," in raw,
            space=sp,
            month_accents=_uses_accents(mw, month, lang, abbr),
        )
        dm = DateMatch(m.start(), m.end(), raw, day, month, year, fmt)
        dm.partial = day is None or year is None
        dm.confidence = 1.0 if not dm.partial else 0.75
        out.append(dm)
    return out


def _scan_years(text: str, exclude: List[DateMatch]) -> List[DateMatch]:
    taken = set()
    for m in exclude:
        taken.update(range(m.start, m.end))
    out: List[DateMatch] = []
    for m in RE_YEAR_ONLY.finditer(text):
        if any(i in taken for i in range(m.start(), m.end())):
            continue
        y = int(m.group("y"))
        dm = DateMatch(m.start(), m.end(), m.group(0), None, None, y,
                       DateFormat(kind="year", order="Y", year_digits=4))
        dm.partial = True
        dm.confidence = 0.4
        out.append(dm)
    return out


def _dedupe(matches: List[DateMatch]) -> List[DateMatch]:
    """Keeps the longest pattern when there is an overlap."""
    matches = sorted(matches, key=lambda m: (m.start, -(m.end - m.start)))
    out: List[DateMatch] = []
    last_end = -1
    for m in matches:
        if m.start >= last_end:
            out.append(m)
            last_end = m.end
        elif m.end > last_end and (m.end - m.start) > (out[-1].end - out[-1].start):
            out[-1] = m
            last_end = m.end
    return out


def find_dates(text: str, include_years: bool = False) -> List[DateMatch]:
    """Every date in a text, deduplicated, longest pattern wins."""
    found = _scan_textual(text) + _scan_numeric(text)
    found = _dedupe(found)
    if include_years:
        found = _dedupe(found + _scan_years(text, found))
    return found


def resolve_ambiguity(matches: List[DateMatch], locale_hint: str = "fr") -> str:
    """
    Infers the day/month convention of the document from its unambiguous dates,
    then applies it to the ones that are ambiguous.

    Returns the convention retained: "DMY", "MDY" or "unknown".
    """
    votes = {"DMY": 0, "MDY": 0}
    for m in matches:
        if m.fmt.kind != "numeric" or m.fmt.order == "YMD":
            continue
        if not m.ambiguous:
            if m.fmt.order in votes:
                votes[m.fmt.order] += 1

    if votes["DMY"] > votes["MDY"]:
        convention = "DMY"
    elif votes["MDY"] > votes["DMY"]:
        convention = "MDY"
    else:
        convention = "unknown"

    effective = convention if convention != "unknown" else (
        "DMY" if locale_hint == "fr" else "MDY")

    for m in matches:
        if not m.ambiguous or m.alt is None:
            continue
        if m.fmt.order != effective:
            m.day, m.month, m.year = m.alt
            m.alt = (m.day, m.month, m.year)
            m.fmt.order = effective
        # the ambiguity stays flagged if the document did not settle it itself
        m.confidence = 0.9 if convention != "unknown" else 0.55

    return convention


# --------------------------------------------------------------------------
# Transformations
# --------------------------------------------------------------------------


def shift_date(d: dt.date, days: int = 0, months: int = 0, years: int = 0) -> dt.date:
    y = d.year + years
    m = d.month + months
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    # last day of the target month if the day does not exist (31 -> 30/28)
    day = d.day
    while day > 28:
        try:
            dt.date(y, m, day)
            break
        except ValueError:
            day -= 1
    return dt.date(y, m, day) + dt.timedelta(days=days)


def apply_rule(match: DateMatch, rule: dict) -> Optional[str]:
    """
    Computes the replacement text for one match.

    Rule types:
      {"type": "set",   "iso": "2025-11-28"}          fixed date
      {"type": "shift", "days":0,"months":0,"years":1} offset
      {"type": "text",  "value": "…"}                  free text (last resort)
      {"type": "parts", "day":…, "month":…, "year":…}  partial replacement
    """
    kind = rule.get("type")

    if kind == "text":
        return rule.get("value")

    if kind == "map":
        # "every date A becomes B", regardless of how each match happens to be
        # written: the normalised key does the pairing, and the format of the
        # match does the rendering.
        pairs = rule.get("pairs") or {}
        target = pairs.get(match.key)
        if not target:
            return None
        parts = str(target).split("-")
        try:
            if len(parts) >= 3:
                y, mo, d = int(parts[0]), int(parts[1]), int(parts[2])
            elif len(parts) == 2:
                y, mo, d = int(parts[0]), int(parts[1]), match.day
            else:
                y, mo, d = int(parts[0]), match.month, match.day
        except ValueError:
            return None
        if d and mo and y and not _valid_dmy(d, mo, y):
            return None
        return match.fmt.render(d, mo, y)

    if kind == "set":
        try:
            target = dt.date.fromisoformat(rule["iso"])
        except Exception:
            return None
        return match.fmt.render(target.day, target.month, target.year)

    if kind == "shift":
        base = match.as_date()
        if base is None:
            # partial date: we shift what we can
            y = match.year
            mo = match.month
            if y is None:
                return None
            yy = y + int(rule.get("years", 0) or 0)
            if mo is not None:
                mm = mo + int(rule.get("months", 0) or 0)
                yy += (mm - 1) // 12
                mm = (mm - 1) % 12 + 1
            else:
                mm = None
            return match.fmt.render(None, mm, yy)
        target = shift_date(base,
                            int(rule.get("days", 0) or 0),
                            int(rule.get("months", 0) or 0),
                            int(rule.get("years", 0) or 0))
        return match.fmt.render(target.day, target.month, target.year)

    if kind == "parts":
        d = rule.get("day", match.day)
        mo = rule.get("month", match.month)
        y = rule.get("year", match.year)
        d = int(d) if d not in (None, "") else None
        mo = int(mo) if mo not in (None, "") else None
        y = int(y) if y not in (None, "") else None
        if d and mo and y and not _valid_dmy(d, mo, y):
            return None
        return match.fmt.render(d, mo, y)

    return None
