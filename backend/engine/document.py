"""
Orchestration: analysis of a document, simulation, application, verification.
This is the layer the API exposes.

Two architectural choices that shape everything else:

1. **Internal state is never serialised** (date format, character references,
   fonts) to the client only to be received back. Everything is recomputed
   server-side from the PDF, the only source of truth. Since enumeration is
   purely deterministic, match ids are stable from one call to the next. The
   client only ever handles ids.

2. **Dates are just one finder among others.** The surgery engine replaces a
   range of characters with another piece of text; it has no idea what a date
   is. Searching for "Jean Dupont", searching for `FA-(\\d{4})` or searching
   for dates are three entry points into the same pipeline.
"""

from __future__ import annotations

import io
import re
import threading
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import fitz
import pikepdf

from .dates import DateMatch, apply_rule, find_dates, resolve_ambiguity
from .fontrepair import FontLibrary, repair_font
from .fonts import ABSENT, FontInfo
from .search import Finder, TextMatch, merge, run_finder
from .surgery import (ALIGNE, AUTO, EXACT, FIT, MOTS, PUSH, REFUS, REGROUPE,
                      Replacement, ReplacementResult, apply_replacements,
                      plan_replacement)
from .textmap import ContentUnit, build_units
from .verify import collect_boxes, new_notdef, search_rects, verify_documents

CONTEXT_RADIUS = 46
MAX_MATCHES = 4000

# Parsing the content streams of a 300-page document costs some fifteen
# seconds. Interactive search restarts that work on every keystroke: without a
# cache, the interface would be unusable. Entries are never mutated — applying,
# for its part, always starts again from a fresh read.
_PARSE_MAX = 3
_parse_cache: "OrderedDict[str, tuple]" = OrderedDict()
_parse_lock = threading.Lock()


def parse_cached(pdf_bytes: bytes, key: Optional[str]):
    """(pikepdf, units, font_cache, fitz) — read-only."""
    if key:
        with _parse_lock:
            hit = _parse_cache.get(key)
            if hit is not None:
                _parse_cache.move_to_end(key)
                return hit

    pdf = _open(pdf_bytes)
    units, fonts = build_units(pdf)
    try:
        fdoc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        pdf.close()
        raise ValueError("Cannot render")
    entry = (pdf, units, fonts, fdoc)

    if key:
        with _parse_lock:
            _parse_cache[key] = entry
            while len(_parse_cache) > _PARSE_MAX:
                _old_key, old = _parse_cache.popitem(last=False)
                for obj in (old[0], old[3]):
                    try:
                        obj.close()
                    except Exception:
                        pass
    return entry


def drop_cached(key: str) -> None:
    with _parse_lock:
        old = _parse_cache.pop(key, None)
    if old:
        for obj in (old[0], old[3]):
            try:
                obj.close()
            except Exception:
                pass


# --------------------------------------------------------------------------
# Exposed model
# --------------------------------------------------------------------------


@dataclass
class Occurrence:
    id: str
    page: int
    unit: str
    start: int
    end: int
    raw: str
    target: str                 # replacement proposed by the rule ("" = none)
    kind: str                   # text | regex | dates
    finder: str                 # id of the rule that found it
    context_before: str
    context_after: str
    font: str
    fonts: List[str]
    invisible: bool
    bbox: Optional[List[float]] = None
    gap_after: Optional[float] = None
    room: Optional[float] = None
    note: Optional[str] = None
    # date-specific fields
    iso: Optional[str] = None
    key: Optional[str] = None
    day: Optional[int] = None
    month: Optional[int] = None
    year: Optional[int] = None
    fmt_label: Optional[str] = None
    ambiguous: bool = False
    partial: bool = False
    confidence: float = 1.0

    key_sig: str = ""           # stable identity, see hit_key()

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Analysis:
    pages: int
    page_sizes: List[List[float]]
    occurrences: List[Occurrence]
    fonts: List[dict]
    convention: str
    warnings: List[str]
    encrypted: bool
    signed: bool
    scanned_pages: List[int]
    ocr_pages: List[int]
    finder_counts: Dict[str, int] = field(default_factory=dict)
    finder_errors: Dict[str, str] = field(default_factory=dict)
    producer: str = ""
    title: str = ""
    text_pages: int = 0
    truncated: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["occurrences"] = [o.to_dict() for o in self.occurrences]
        return d


@dataclass
class Outcome:
    results: List[dict] = field(default_factory=list)
    repairs: List[dict] = field(default_factory=list)
    verification: Optional[dict] = None
    pdf_bytes: Optional[bytes] = None
    applied: int = 0
    refused: int = 0
    strategy_counts: Dict[str, int] = field(default_factory=dict)
    fit_counts: Dict[str, int] = field(default_factory=dict)
    touched_pages: List[int] = field(default_factory=list)
    # Blank boxes that appeared. Blind spot of pixel verification: they crop up
    # right where a change is expected, hence inside the mask.
    notdef: List[dict] = field(default_factory=list)
    # Expected modification regions, per page. Kept so that the control image
    # shows *exactly* the mask that was used for verification: two different
    # masks would give two different verdicts.
    page_boxes: Dict[int, List[List[float]]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "results": self.results, "repairs": self.repairs,
            "verification": self.verification, "applied": self.applied,
            "refused": self.refused, "strategy_counts": self.strategy_counts,
            "fit_counts": self.fit_counts, "touched_pages": self.touched_pages,
            "notdef": self.notdef,
            "page_boxes": {str(k): v for k, v in self.page_boxes.items()},
        }


# --------------------------------------------------------------------------
# Page geometry
# --------------------------------------------------------------------------


@dataclass
class PageGeometry:
    """Words of the page, in the render frame, plus the edges of the text area."""

    words: List[Tuple[float, float, float, float]] = field(default_factory=list)
    right_limit: float = 0.0
    page_width: float = 0.0

    def measure(self, bbox: Sequence[float]) -> Tuple[Optional[float], float]:
        """
        (blank space up to the next word on the line, room up to the right edge).

        This is what makes it possible to tell a word in the flow of a sentence
        — which should be allowed to push the line — from a piece of text alone
        in a cell, whose region must be preserved.
        """
        x0, y0, x1, y1 = bbox[0], bbox[1], bbox[2], bbox[3]
        cy = (y0 + y1) / 2.0
        same_line = [w for w in self.words if w[1] < cy < w[3]]
        if not same_line:
            return None, max(0.0, self.right_limit - x1)

        after = [w for w in same_line if w[0] >= x1 - 0.5]
        gap = (min(w[0] for w in after) - x1) if after else None
        line_end = max(w[2] for w in same_line)
        return gap, max(0.0, self.right_limit - line_end)

    def line_box(self, bbox: Sequence[float]) -> List[float]:
        """Rectangle covering the end of the line, starting from the segment.

        When a replacement pushes the rest of the line, that region *must*
        change: it is therefore part of the expected modifications, otherwise
        the integrity check would report a drift that is in fact intended.
        """
        x0, y0, x1, y1 = bbox[0], bbox[1], bbox[2], bbox[3]
        cy = (y0 + y1) / 2.0
        same_line = [w for w in self.words if w[1] < cy < w[3]]
        top = min([w[1] for w in same_line], default=y0)
        bot = max([w[3] for w in same_line], default=y1)
        # Up to the edge of the page: text that pushes may very well spill past
        # the margin, and that region is expected too.
        right = max(self.page_width, self.right_limit, x1)
        # Vertical padding: the neighbouring glyphs on the line may rise higher
        # or drop lower than the replaced segment (parentheses, underscores,
        # accented capitals). Without it, their displacement falls outside the
        # mask and is wrongly reported as drift.
        pad = max(1.5, (y1 - y0) * 0.45)
        return [min(x0, x1), min(top, y0) - pad, right, max(bot, y1) + pad]


_geom_cache: "OrderedDict[str, PageGeometry]" = OrderedDict()
_GEOM_MAX = 1200


def page_geometry_cached(page, key: Optional[str], pno: int) -> PageGeometry:
    """Geometry does not depend on the search rules: it is computed once per
    page and reused on every keystroke of the user."""
    if not key:
        return page_geometry(page)
    ck = "%s#%d" % (key, pno)
    with _parse_lock:
        hit = _geom_cache.get(ck)
        if hit is not None:
            _geom_cache.move_to_end(ck)
            return hit
    geom = page_geometry(page)
    with _parse_lock:
        _geom_cache[ck] = geom
        while len(_geom_cache) > _GEOM_MAX:
            _geom_cache.popitem(last=False)
    return geom


def page_geometry(page) -> PageGeometry:
    try:
        m = page.rotation_matrix
    except Exception:
        m = None
    words: List[Tuple[float, float, float, float]] = []
    try:
        for w in page.get_text("words"):
            r = fitz.Rect(w[0], w[1], w[2], w[3])
            if m is not None:
                r = r * m
            words.append((min(r.x0, r.x1), min(r.y0, r.y1),
                          max(r.x0, r.x1), max(r.y0, r.y1)))
    except Exception:
        pass
    try:
        width = float(page.rect.width)
    except Exception:
        width = 0.0

    if words:
        # The observed right edge underestimates the usable area when the last
        # line of a paragraph is short. We correct it with the assumption of
        # symmetric margins, very reliable on a laid-out document.
        observed = max(w[2] for w in words)
        left = min(w[0] for w in words)
        right = max(observed, width - left) if width else observed
    else:
        right = width * 0.94
    return PageGeometry(words=words, right_limit=right, page_width=width)


# --------------------------------------------------------------------------
# Utilities
# --------------------------------------------------------------------------


def gap_to_next_glyph(unit: ContentUnit, end: int, page,
                      bbox: Sequence[float]) -> Optional[float]:
    """
    Distance to the next **glyph** on the same line, in points.

    Definitely not to the next word: MuPDF considers "hwloc-bind" a single
    word, so the measured blank was the one after the whole token. As a result,
    replacing "hwloc" with "topolib" crushed the hyphen and produced
    "topolibbind". We therefore measure on our own extraction, which knows the
    exact position of every drawn character.

    None means "nothing left on this line".
    """
    n = len(unit.char_refs)
    h = max(1.0, bbox[3] - bbox[1])
    cy = (bbox[1] + bbox[3]) / 2.0
    i = end
    while i < n and unit.text[i] != "\n":
        if unit.char_refs[i] is not None:
            nb = occurrence_bbox(unit, i, i + 1, page)
            if not nb:
                return None
            # The next character must be on the same line and to the right,
            # otherwise the segment is at the end of a line or of a column.
            if abs((nb[1] + nb[3]) / 2.0 - cy) > h * 0.6:
                return None
            return max(0.0, nb[0] - bbox[2])
        i += 1
    return None


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _detect_signed(pdf: pikepdf.Pdf) -> bool:
    try:
        acro = pdf.Root.get("/AcroForm")
        if acro is None:
            return False
        sf = acro.get("/SigFlags")
        if sf is not None and int(sf) & 1:
            return True
        fields = acro.get("/Fields")
        if fields is not None:
            for f in fields:
                if str(f.get("/FT") or "") == "/Sig":
                    return True
    except Exception:
        pass
    return False


def _page_is_scan(fdoc: fitz.Document, pno: int) -> Tuple[bool, bool]:
    try:
        page = fdoc[pno]
        text = page.get_text("text").strip()
        has_image = bool(page.get_images(full=True))
        if not text:
            return has_image, False
        try:
            spans = page.get_texttrace() or []
            invisible = bool(spans) and all(int(s.get("type", 0)) == 3 for s in spans)
        except Exception:
            invisible = False
        return False, (invisible and has_image)
    except Exception:
        return False, False


# --------------------------------------------------------------------------
# Unified enumeration
# --------------------------------------------------------------------------


def hit_key(page: int, unit_id: str, start: int, raw: str) -> str:
    """
    *Stable* identity of a match, independent of the enumeration rank.

    The ids (`p0-1`, `p0-2`…) are assigned in document order and shift as soon
    as a rule changes. Anything that must survive a change of rules — first and
    foremost the matches the user has unchecked — is therefore located by its
    real position in the stream.
    """
    return "%d\u0001%s\u0001%d\u0001%s" % (page, unit_id, start, raw)


@dataclass
class _Hit:
    ident: str
    unit: ContentUnit
    start: int
    end: int
    raw: str
    kind: str
    finder: str
    target: str
    date: Optional[DateMatch] = None


def _enumerate(units: List[ContentUnit], finders: Sequence[Finder],
               date_rule: Optional[dict], locale_hint: str
               ) -> Tuple[List[_Hit], str, Dict[str, int], bool]:
    """
    Runs every finder over every unit, in a stable order.

    The order is: units in document order, then position within the unit. It
    does not depend on the order of the rules, which guarantees that the ids
    stay the same between the simulation and the application.
    """
    active = [f for f in finders if f.enabled]
    wants_dates = any(f.kind == "dates" for f in active)

    # All dates must be collected first, so that the day/month convention of
    # the document can be deduced before any target is computed.
    date_hits: Dict[str, List[DateMatch]] = {}
    all_dates: List[DateMatch] = []
    if wants_dates:
        include_years = any(f.kind == "dates" and f.query == "years" for f in active)
        for u in units:
            if u.parse_error:
                continue
            ms = find_dates(u.text, include_years=include_years)
            date_hits[u.unit_id] = ms
            all_dates.extend(ms)
    convention = resolve_ambiguity(all_dates, locale_hint) if all_dates else "unknown"

    date_finder = next((f for f in active if f.kind == "dates"), None)

    hits: List[_Hit] = []
    counts: Dict[str, int] = {f.id: 0 for f in active}
    truncated = False
    n = 0

    for u in units:
        if u.parse_error:
            continue

        raw_matches: List[TextMatch] = []
        for f in active:
            if f.kind == "dates":
                continue
            raw_matches.extend(run_finder(f, u.text))

        if wants_dates and date_finder is not None:
            for m in date_hits.get(u.unit_id, []):
                target = apply_rule(m, date_rule) if date_rule else None
                raw_matches.append(TextMatch(
                    finder_id=date_finder.id, start=m.start, end=m.end,
                    raw=m.raw, target=target or "", kind="dates"))

        for tm in merge(raw_matches):
            if len(hits) >= MAX_MATCHES:
                truncated = True
                break
            n += 1
            date_obj = None
            if tm.kind == "dates":
                date_obj = next((d for d in date_hits.get(u.unit_id, [])
                                 if d.start == tm.start and d.end == tm.end), None)
            hits.append(_Hit(ident="%s-%d" % (u.unit_id, n), unit=u,
                             start=tm.start, end=tm.end, raw=tm.raw,
                             kind=tm.kind, finder=tm.finder_id,
                             target=tm.target, date=date_obj))
            counts[tm.finder_id] = counts.get(tm.finder_id, 0) + 1
        if truncated:
            break

    return hits, convention, counts, truncated


def _match_fonts(u: ContentUnit, start: int, end: int):
    try:
        refs = [r for r in u.char_refs[start:end] if r is not None]
        ops = [u.ops[r.op_index] for r in refs]
        if not ops:
            return [], False, None
        return (sorted({o.font.display_name for o in ops}),
                all(o.tmode == 3 for o in ops), ops[0].font)
    except Exception:
        return [], False, None


def _page_transform(page):
    """
    Complete matrix: PDF user space -> render space.

    MuPDF's `transformation_matrix` encapsulates both the cropping (a CropBox
    different from the MediaBox shifts the whole page, and some documents abuse
    it) and the flip of the vertical axis. `rotation_matrix` composes with it to
    give the frame of the image actually rendered. Doing that computation by
    hand amounts to reimplementing both, with the bugs thrown in.
    """
    try:
        return page.transformation_matrix * page.rotation_matrix
    except Exception:
        try:
            return page.transformation_matrix
        except Exception:
            return None


def occurrence_bbox(unit: ContentUnit, start: int, end: int, page,
                    extra: float = 0.0) -> Optional[List[float]]:
    """
    Exact rectangle of a segment, computed from the matrices of the stream.

    MuPDF's `search_for` is deliberately not used: that search is
    case-insensitive, so "Memcheck" and "memcheck" get mixed up and the pairing
    of rectangles goes haywire as soon as a document contains both. Here the
    position comes from the stream itself, so it is exact and free of any
    possible ambiguity.
    """
    try:
        refs = [r for r in unit.char_refs[start:end] if r is not None]
        if not refs:
            return None
        boxes = []
        by_op: Dict[int, List[int]] = {}
        for r in refs:
            by_op.setdefault(r.op_index, []).append(r.code_index)
        last_op = max(by_op)
        for op_index, idxs in by_op.items():
            op = unit.ops[op_index]
            # Any lengthening happens at the end of the segment: it is only
            # added to the last operator involved.
            boxes.append(op.bbox(min(idxs), max(idxs) + 1,
                                 extra if op_index == last_op else 0.0))
        x0 = min(b[0] for b in boxes); y0 = min(b[1] for b in boxes)
        x1 = max(b[2] for b in boxes); y1 = max(b[3] for b in boxes)
    except Exception:
        return None

    m = _page_transform(page)
    if m is None:
        return None
    try:
        r = fitz.Rect(x0, y0, x1, y1) * m
        a = [min(r.x0, r.x1), min(r.y0, r.y1), max(r.x0, r.x1), max(r.y0, r.y1)]
    except Exception:
        return None
    return [round(v, 2) for v in a]


def _assign_boxes(fdoc: fitz.Document, hits: Sequence[_Hit]) -> Dict[str, List[float]]:
    pages: Dict[int, object] = {}
    out: Dict[str, List[float]] = {}
    for h in hits:
        pno = h.unit.page_number
        if pno not in pages:
            try:
                pages[pno] = fdoc[pno]
            except Exception:
                pages[pno] = None
        page = pages[pno]
        if page is None:
            continue
        b = occurrence_bbox(h.unit, h.start, h.end, page)
        if b:
            out[h.ident] = b
    return out


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------


def _open(pdf_bytes: bytes):
    try:
        pdf = pikepdf.open(io.BytesIO(pdf_bytes))
    except pikepdf.PasswordError:
        raise ValueError("This PDF is password-protected.")
    except Exception as e:
        raise ValueError("Unreadable PDF: %s" % e)
    return pdf


def analyze(pdf_bytes: bytes, finders: Sequence[Finder],
            date_rule: Optional[dict] = None, locale_hint: str = "fr",
            cache_key: Optional[str] = None) -> Analysis:
    warnings: List[str] = []
    pdf, units, font_cache, fdoc = parse_cached(pdf_bytes, cache_key)

    encrypted = bool(pdf.is_encrypted)
    if encrypted:
        warnings.append("Encrypted document: it will be rewritten unencrypted, "
                        "the printing and copying restrictions will be lost.")
    signed = _detect_signed(pdf)
    if signed:
        warnings.append("Digital signature detected. Any change will "
                        "invalidate it — that is what a signature is for.")

    for u in units:
        if u.parse_error:
            warnings.append("Unreadable content stream (%s): %s"
                            % (u.unit_id, u.parse_error))

    finder_errors = {}
    for f in finders:
        err = f.error()
        if err:
            finder_errors[f.id] = err

    hits, convention, counts, truncated = _enumerate(
        units, finders, date_rule, locale_hint)
    boxes = _assign_boxes(fdoc, hits)

    geoms: Dict[int, PageGeometry] = {}
    for h in hits:
        if h.unit.page_number not in geoms:
            try:
                geoms[h.unit.page_number] = page_geometry_cached(
                    fdoc[h.unit.page_number], cache_key, h.unit.page_number)
            except Exception:
                geoms[h.unit.page_number] = PageGeometry()

    occurrences: List[Occurrence] = []
    for h in hits:
        fonts, invisible, _fi = _match_fonts(h.unit, h.start, h.end)
        bbox = boxes.get(h.ident)
        gap = room = None
        if bbox:
            _g, room = geoms[h.unit.page_number].measure(bbox)
            gap = gap_to_next_glyph(h.unit, h.end, fdoc[h.unit.page_number], bbox)

        occ = Occurrence(
            id=h.ident, page=h.unit.page_number, unit=h.unit.unit_id,
            start=h.start, end=h.end, raw=h.raw, target=h.target,
            kind=h.kind, finder=h.finder,
            context_before=_clean(h.unit.text[max(0, h.start - CONTEXT_RADIUS):h.start]),
            context_after=_clean(h.unit.text[h.end:h.end + CONTEXT_RADIUS]),
            font=fonts[0] if fonts else "?", fonts=fonts,
            invisible=invisible, bbox=bbox,
            key_sig=hit_key(h.unit.page_number, h.unit.unit_id, h.start, h.raw),
            gap_after=round(gap, 2) if gap is not None else None,
            room=round(room, 2) if room is not None else None,
        )
        if h.date is not None:
            d = h.date
            occ.iso, occ.key = d.iso, d.key
            occ.day, occ.month, occ.year = d.day, d.month, d.year
            occ.fmt_label = d.fmt.describe()
            occ.ambiguous, occ.partial = d.ambiguous, d.partial
            occ.confidence = round(d.confidence, 2)
        if invisible:
            occ.note = ("Invisible OCR layer laid over an image: changing this "
                        "text does not change what is visible.")
        elif h.unit.kind == "xobject" and h.unit.instance_count > 1:
            occ.note = ("Reused block, drawn %d times in the document. "
                        "The change will apply everywhere." % h.unit.instance_count)
        occurrences.append(occ)

    if truncated:
        warnings.append("More than %d matches: the list has been truncated. "
                        "Narrow down your search." % MAX_MATCHES)

    scanned, ocr, text_pages = [], [], 0
    for pno in range(fdoc.page_count):
        s, o = _page_is_scan(fdoc, pno)
        if s:
            scanned.append(pno)
        else:
            text_pages += 1
        if o:
            ocr.append(pno)

    if scanned:
        warnings.append(
            "Page(s) %s: no text layer, these are images. Nothing there can be "
            "changed without OCR."
            % ", ".join(str(p + 1) for p in scanned))
    if ocr:
        warnings.append(
            "Page(s) %s: the text is an invisible OCR layer on top of an "
            "image. Changing it does not change the render."
            % ", ".join(str(p + 1) for p in ocr))

    fonts_summary = []
    for fi in font_cache.values():
        s = fi.summary()
        fonts_summary.append(s)
        if not s["editable"]:
            warnings.append('Font "%s" is not editable: %s'
                            % (s["name"], s["reason"]))

    page_sizes = [[round(fdoc[p].rect.width, 2), round(fdoc[p].rect.height, 2)]
                  for p in range(fdoc.page_count)]
    meta = fdoc.metadata or {}
    out = Analysis(
        pages=fdoc.page_count, page_sizes=page_sizes, occurrences=occurrences,
        fonts=fonts_summary, convention=convention, warnings=warnings,
        encrypted=encrypted, signed=signed, scanned_pages=scanned, ocr_pages=ocr,
        finder_counts=counts, finder_errors=finder_errors,
        producer=(meta.get("producer") or "")[:140],
        title=(meta.get("title") or "")[:140], text_pages=text_pages,
        truncated=truncated,
    )
    return out


# --------------------------------------------------------------------------
# Simulation and application
# --------------------------------------------------------------------------


def process(pdf_bytes: bytes, finders: Sequence[Finder],
            date_rule: Optional[dict] = None,
            overrides: Optional[Dict[str, str]] = None,
            selection: Optional[Sequence[str]] = None,
            exclude: Optional[Sequence[str]] = None,
            fits: Optional[Dict[str, str]] = None,
            anchors: Optional[Dict[str, str]] = None,
            locale_hint: str = "fr", auto_repair: bool = True,
            verify: bool = True, dry_run: bool = False,
            font_library: Optional[FontLibrary] = None,
            extra_font_paths: Optional[Dict[str, str]] = None,
            cache_key: Optional[str] = None) -> Outcome:
    """
    Computes the replacements, then (unless `dry_run`) applies and verifies them.

    `overrides` {id: forced text} takes precedence over the finder's rule.
    `fits`      {id: auto|exact|fit|push} forces the width handling.
    `exclude`   stable identities (see hit_key) *not* to process.

    Removal is done by exclusion, never by an allowlist. An allowlist computed
    by the client becomes wrong as soon as its rules change: it is enough to
    apply while a search is still in flight for the server to receive one rule
    more and a selection from before — and silently drop the new match. By
    excluding only what is explicitly unchecked, a synchronisation lag can no
    longer remove anything at all.
    """
    overrides = overrides or {}
    fits = fits or {}
    anchors = anchors or {}
    sel = set(selection) if selection is not None else None
    excluded = set(exclude or ())

    # Applying modifies the document: it always starts again from a fresh read
    # so as never to dirty a shared cache entry.
    if dry_run:
        pdf, units, font_cache, fdoc = parse_cached(pdf_bytes, cache_key)
        owned = False
    else:
        pdf = _open(pdf_bytes)
        units, font_cache = build_units(pdf)
        fdoc = fitz.open(stream=pdf_bytes, filetype="pdf")
        owned = True

    hits, _conv, _counts, _tr = _enumerate(units, finders, date_rule, locale_hint)

    geoms: Dict[int, PageGeometry] = {}
    boxes: Dict[str, List[float]] = {}
    try:
        boxes = _assign_boxes(fdoc, hits)
        for h in hits:
            if h.unit.page_number not in geoms:
                geoms[h.unit.page_number] = page_geometry_cached(
                    fdoc[h.unit.page_number], cache_key if dry_run else None,
                    h.unit.page_number)
    except Exception:
        pass

    by_unit: Dict[str, List[Replacement]] = {}
    hit_by_id: Dict[str, _Hit] = {h.ident: h for h in hits}

    for h in hits:
        if hit_key(h.unit.page_number, h.unit.unit_id, h.start, h.raw) in excluded:
            continue
        if sel is not None and h.ident not in sel:
            continue
        target = overrides.get(h.ident, h.target)
        if not target or target == h.raw:
            continue
        gap = room = None
        bbox = boxes.get(h.ident)
        if bbox and h.unit.page_number in geoms:
            _g, room = geoms[h.unit.page_number].measure(bbox)
            try:
                gap = gap_to_next_glyph(h.unit, h.end, fdoc[h.unit.page_number], bbox)
            except Exception:
                gap = _g
        by_unit.setdefault(h.unit.unit_id, []).append(Replacement(
            unit_id=h.unit.unit_id, start=h.start, end=h.end, new_text=target,
            anchor=anchors.get(h.ident, "left"), fit=fits.get(h.ident, AUTO),
            ident=h.ident, gap_after=gap, room=room))

    unit_by_id = {u.unit_id: u for u in units}
    page_of_unit = {u.unit_id: u.page_number for u in units}

    repairs: List[dict] = []
    if auto_repair and not dry_run:
        repairs = _repair_pass(pdf, unit_by_id, by_unit, font_cache,
                               font_library, extra_font_paths)

    if dry_run:
        results = []
        cache: Dict[str, Optional[str]] = {}
        for uid, reps in by_unit.items():
            u = unit_by_id[uid]
            for rep in reps:
                r = plan_replacement(u, rep)
                d = _result_dict(r, page_of_unit.get(uid, 0))
                if not r.ok and r.missing_glyphs:
                    d["repairable"] = _can_repair(u, rep, font_library,
                                                  extra_font_paths, cache)
                results.append(d)
        return _summarize(results, [], None, None)

    all_results: List[ReplacementResult] = []
    for uid, reps in by_unit.items():
        res, _changed = apply_replacements(pdf, unit_by_id[uid], reps)
        all_results.extend(res)

    buf = io.BytesIO()
    pdf.save(buf, linearize=False)
    out_bytes = buf.getvalue()
    if owned:
        pdf.close()

    # --- verification masks -----------------------------------------------
    page_boxes: Dict[int, List[List[float]]] = {}
    try:
        before = fitz.open(stream=pdf_bytes, filetype="pdf")
        after = fitz.open(stream=out_bytes, filetype="pdf")
        for r in all_results:
            if not r.ok:
                continue
            pno = page_of_unit.get(r.unit_id, 0)
            box = page_boxes.setdefault(pno, [])
            h = hit_by_id.get(r.ident)

            # The expected region is computed, not searched for: we project the
            # original box lengthened by the advance width delta. Searching for
            # the new string in the produced document would be both slow and
            # wrong (MuPDF's search ignores case).
            if h is not None:
                grown = occurrence_bbox(h.unit, h.start, h.end, before[pno],
                                        extra=max(0.0, r.width_delta) + 1.0)
                if grown:
                    box.append(grown)
            bbox = boxes.get(r.ident)
            if bbox:
                box.append(bbox)
            # A replacement that pushes the line moves everything that follows:
            # it is intended, hence expected, hence masked.
            if r.reflow and bbox and pno in geoms:
                box.append(geoms[pno].line_box(bbox))
        before.close()
        after.close()
    except Exception:
        pass
    if owned:
        try:
            fdoc.close()
        except Exception:
            pass

    verification = None
    if verify and page_boxes:
        verification = verify_documents(pdf_bytes, out_bytes, page_boxes).to_dict()

    # --- blank boxes ------------------------------------------------------
    tofu: List[dict] = []
    if verify:
        try:
            tofu = new_notdef(pdf_bytes, out_bytes)
        except Exception:
            tofu = []

    results = [_result_dict(r, page_of_unit.get(r.unit_id, 0)) for r in all_results]

    if tofu:
        _attribute_notdef(tofu, results, boxes)
        if verification is not None:
            # A blank box is an outright failure, whatever the pixel count may
            # say: the produced document is visibly broken.
            verification["severity"] = "drift"
            verification["acceptable"] = False
            verification["notdef_total"] = len(tofu)

    outcome = _summarize(results, repairs, verification, out_bytes,
                         sorted(page_boxes.keys()))
    outcome.page_boxes = page_boxes
    outcome.notdef = tofu
    return outcome


def _attribute_notdef(tofu: List[dict], results: List[dict],
                      boxes: Dict[str, List[float]]) -> None:
    """Attaches each blank box to the replacement that produced it."""
    by_id = {r["id"]: r for r in results}
    for e in tofu:
        bb = e.get("bbox")
        if not bb:
            continue
        best = None
        for ident, ob in boxes.items():
            r = by_id.get(ident)
            if r is None or r.get("page") != e["page"] or not r.get("ok"):
                continue
            # Does the blank box fall inside the region of the replacement,
            # widened by any lengthening?
            x1 = ob[2] + max(0.0, r.get("width_delta") or 0.0) + 2.0
            if ob[0] - 2 <= bb[0] and bb[2] <= x1 and \
                    ob[1] - 2 <= (bb[1] + bb[3]) / 2 <= ob[3] + 2:
                best = r
                break
        if best is not None:
            e["result_id"] = best["id"]
            best.setdefault("notdef_chars", []).append(e["char"])
            best["ok"] = False
            best["strategy"] = REFUS
            best["reason"] = ('the render produces a blank box for "%s": the '
                              'embedded font declares this character but does '
                              'not draw it'
                              % "".join(dict.fromkeys(best["notdef_chars"])))


def _summarize(results, repairs, verification, pdf_bytes, touched=None) -> Outcome:
    counts: Dict[str, int] = {}
    fits: Dict[str, int] = {}
    for r in results:
        counts[r["strategy"]] = counts.get(r["strategy"], 0) + 1
        if r["ok"]:
            fits[r["fit_used"]] = fits.get(r["fit_used"], 0) + 1
    return Outcome(
        results=sorted(results, key=lambda r: (r["page"], r["id"])),
        repairs=repairs, verification=verification, pdf_bytes=pdf_bytes,
        applied=sum(1 for r in results if r["ok"]),
        refused=sum(1 for r in results if not r["ok"]),
        strategy_counts=counts, fit_counts=fits,
        touched_pages=touched if touched is not None
        else sorted({r["page"] for r in results}),
    )


def _can_repair(u, rep, library, extra_font_paths, cache) -> Optional[str]:
    """Name of the source font that would make the repair possible, or None."""
    _f, _inv, fi = _match_fonts(u, rep.start, rep.end)
    if fi is None or not fi.embedded or fi.font_file_key != "FontFile2":
        return None
    if fi.objgen in cache:
        return cache[fi.objgen]
    answer: Optional[str] = None
    for key in (fi.display_name, fi.base_font):
        if extra_font_paths and key in extra_font_paths:
            answer = "uploaded font"
            break
    if answer is None:
        try:
            from .fontrepair import default_library
            lib = library or default_library()
            entry = lib.find(fi.base_font or fi.display_name)
            if entry is not None:
                answer = entry.ps_name or entry.full_name or entry.family
        except Exception:
            answer = None
    cache[fi.objgen] = answer
    return answer


def _repair_pass(pdf, unit_by_id, by_unit, font_cache, font_library,
                 extra_font_paths) -> List[dict]:
    needed: Dict[str, set] = {}
    for uid, reps in by_unit.items():
        u = unit_by_id[uid]
        for rep in reps:
            r = plan_replacement(u, rep)
            if r.ok or not r.missing_glyphs:
                continue
            _f, _inv, fi = _match_fonts(u, rep.start, rep.end)
            if fi is None:
                continue
            needed.setdefault(fi.objgen, set()).update(r.missing_glyphs)

    if not needed:
        return []

    objgen_to_obj = _index_font_objects(pdf)
    repairs: List[dict] = []
    for objgen, chars in needed.items():
        fi = font_cache.get(objgen)
        fobj = objgen_to_obj.get(objgen)
        if fi is None or fobj is None:
            repairs.append({"font": "?", "ok": False, "requested": sorted(chars),
                            "added": [], "reason": "font not found in the resources"})
            continue
        src = None
        for key in (fi.display_name, fi.base_font):
            if extra_font_paths and key in extra_font_paths:
                src = extra_font_paths[key]
                break
        rr = repair_font(pdf, fobj, fi, sorted(chars),
                         library=font_library, source_path=src)
        repairs.append({
            "font": fi.display_name, "requested": sorted(chars), "ok": rr.ok,
            "added": rr.added, "source": rr.source_name,
            "exact_source": rr.exact_source, "reason": rr.reason,
        })
    return repairs


def _result_dict(r: ReplacementResult, page: int) -> dict:
    return {
        "id": r.ident, "page": page, "unit": r.unit_id,
        "old": r.old_text, "new": r.new_text,
        "strategy": r.strategy, "label": r.label, "ok": r.ok, "reason": r.reason,
        "fonts": r.font_names, "coverage": r.coverage, "missing": r.missing_glyphs,
        "width_old": r.width_old, "width_new": r.width_new,
        "width_delta": r.width_delta, "compensated": r.compensated,
        "fit_used": r.fit_used, "fit_reason": r.fit_reason,
        "scaled": r.scaled, "reflow": r.reflow, "overlap_risk": r.overlap_risk,
        "invisible": r.invisible_text, "shared": r.shared_xobject,
    }


def _index_font_objects(pdf: pikepdf.Pdf) -> Dict[str, object]:
    out: Dict[str, object] = {}
    seen_forms = set()

    def visit(res, depth=0):
        if res is None or depth > 6:
            return
        try:
            fonts = res.get("/Font")
            if fonts is not None:
                for _k, v in fonts.items():
                    try:
                        out["%d_%d" % v.objgen] = v
                    except Exception:
                        pass
            xo = res.get("/XObject")
            if xo is not None:
                for _k, v in xo.items():
                    try:
                        if str(v.get("/Subtype") or "") != "/Form":
                            continue
                        key = "%d_%d" % v.objgen
                        if key in seen_forms:
                            continue
                        seen_forms.add(key)
                        visit(v.get("/Resources"), depth + 1)
                    except Exception:
                        pass
        except Exception:
            pass

    for page in pdf.pages:
        try:
            visit(page.get("/Resources"))
        except Exception:
            continue
    return out
