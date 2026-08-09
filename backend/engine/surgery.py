"""
Surgical rewriting of the content stream.

Four strategies, from the most faithful to the least. We always take the
highest one possible, and we say which one was used.

  ALIGNE     Same number of characters and unchanged advance width: every code
             is replaced *in place*, in its original operator, without touching
             a single kern. The rendering is mathematically identical outside
             the glyphs themselves. This is the nominal case for a date
             replacement, since digits are tabular in almost every font.

  MOTS       The source separates its words with kerning rather than with a
             space glyph — the normal case for PDFs produced by TeX, whose
             fonts often embed no space at all. Each word is rewritten in its
             own operator, the original blanks stay intact, and no space glyph
             is ever required.

  REGROUPE   Different length or different width. The span is rewritten as one
             block, and the width is made up for (see below).

  REFUS      Glyph missing from the embedded font, font not editable, exotic
             encoding. We do not silently produce a blank box: we refuse and we
             explain.

--------------------------------------------------------------------------
The width problem
--------------------------------------------------------------------------

For dates the problem does not exist: digits are tabular, the width does not
move. For arbitrary text it becomes central. Replacing "Jean Dupont" with
"Christophe-Alexandre de La Tour" is 114 points more. Three behaviours, all
legitimate — but dictated by *context*, not by preference:

  exact   Compensation by kerning: the rest of the line does not move by a
          thousandth. The text overflows if it is wider. Correct in a table
          cell or a form field when it fits.

  fit     Horizontal stretch (Tz) to occupy exactly the original width. Layout
          preserved to the point, at the cost of condensing the glyphs. Beyond
          ~25 % compression it shows.

  push    No compensation: the text takes its natural width and pushes what
          follows. It is what a word processor would do, and it is **almost
          always what you want in the middle of a sentence**.

The `auto` mode decides from the real geometry of the page, which the caller
supplies in `Replacement`:

  gap_after  distance to the next word on the same line
  room       distance to the right edge of the page's text area

The scale of decisions:

  1. negligible difference                  -> exact
  2. imperceptible stretch (±3 %)           -> fit
  3. big blank after (cell, column)         -> exact if it fits, otherwise fit
  4. running text, the line has margin      -> push
  5. otherwise                              -> fit if legible, else push flagged

None of these decisions is imposed: the interface exposes them and the user can
force any mode, match by match.

--------------------------------------------------------------------------
The formulas, once and for all
--------------------------------------------------------------------------

Advance of one glyph:        (w/1000·Tfs + Tc + Tw_if_space) · Th/100
Displacement of a TJ number: −n/1000 · Tfs · Th/100

Calling "raw advance" the parenthesis without the Th factor:

  compensation  n = (raw_new − raw_old) · 1000 / Tfs        (Th cancels out)
  fit           Th_new = Th_old · raw_old / raw_new

Placing n *after* the text pins it left, *before* pins it right.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import pikepdf

from .fonts import ABSENT, CERTAIN, DECLARE, INCERTAIN, PROGRAMME, FontInfo
from .textmap import ContentUnit, ShowOp

ALIGNE = "inplace"
MOTS = "words"
REGROUPE = "recomposed"
REFUS = "blocked"

_LEVEL_LABEL = {
    ALIGNE: "pixel-identical",
    MOTS: "recomposed word by word",
    REGROUPE: "recomposed with compensation",
    REFUS: "not possible",
}

EXACT = "exact"
FIT = "fit"
PUSH = "push"
AUTO = "auto"

# Below this, a width difference is rounding noise: we compensate for it with
# kerning and never stretch the glyphs.
_NEGLIGIBLE_PT = 0.05

# Stretch invisible to the eye: we prefer it to any displacement.
_SCALE_IMPERCEPTIBLE = 0.03

# Below this, the condensing plainly reads as a defect.
_SCALE_FLOOR = 0.75

# A blank of this size (in ems) after the span signals a zone of its own —
# cell, column, field — rather than a word in the flow of a sentence.
_GAP_EM_THRESHOLD = 2.5

# We never fill a blank down to the last point: glyphs overflow their advance
# width slightly (stems, italics, negative kerns) and text that comes to touch
# its neighbour is spotted immediately.
_GAP_SAFETY_PT = 1.5


@dataclass
class Replacement:
    """A replacement request, expressed in unit text coordinates."""

    unit_id: str
    start: int
    end: int
    new_text: str
    anchor: str = "left"        # left | right | center
    fit: str = AUTO             # auto | exact | fit | push
    ident: str = ""             # client-side identifier
    # Geometry measured on the rendered page, in points. Supplied by the
    # document layer, which alone knows the layout.
    gap_after: Optional[float] = None
    room: Optional[float] = None


@dataclass
class ReplacementResult:
    ident: str
    unit_id: str
    old_text: str
    new_text: str
    strategy: str
    ok: bool
    reason: Optional[str] = None
    # diagnostics
    font_names: List[str] = field(default_factory=list)
    coverage: str = CERTAIN
    missing_glyphs: List[str] = field(default_factory=list)
    width_old: float = 0.0      # in points
    width_new: float = 0.0
    width_delta: float = 0.0
    compensated: bool = False
    fit_used: str = EXACT            # mode kept by the automatic decision
    fit_reason: str = ""             # why, in plain words
    scaled: Optional[float] = None   # stretch ratio applied (1.0 = none)
    reflow: bool = False             # the rest of the line was displaced
    overlap_risk: bool = False       # wider with no fit -> overlap
    invisible_text: bool = False     # OCR layer: the rendering will not change
    shared_xobject: int = 1

    @property
    def label(self) -> str:
        return _LEVEL_LABEL.get(self.strategy, self.strategy)


def _decide_fit(delta_pt: float, scale: Optional[float], gap_after: Optional[float],
                room: Optional[float], em: float) -> Tuple[str, str]:
    """
    Picks the width handling from the real geometry.
    Returns (mode, readable explanation).
    """
    if abs(delta_pt) < _NEGLIGIBLE_PT:
        return EXACT, "negligible width difference"

    if scale is not None and abs(scale - 1.0) <= _SCALE_IMPERCEPTIBLE:
        return FIT, "stretch of %.1f %%, invisible to the eye" % ((scale - 1) * 100)

    if gap_after is None:
        # Nothing follows on the line: growing longer bothers nobody, as long
        # as we do not leave the text area.
        if delta_pt <= 0 or room is None or delta_pt <= room:
            return PUSH, "nothing follows on the line"
        if scale is not None and scale >= _SCALE_FLOOR:
            return FIT, "would run past the margin: condensed to %.0f %%" % (scale * 100)
        return PUSH, "runs past the margin by %.0f pt" % (delta_pt - room)

    isolated = em > 0 and gap_after >= _GAP_EM_THRESHOLD * em

    if isolated:
        # The span occupies a zone of its own: we preserve that zone.
        if delta_pt <= 0:
            return EXACT, "isolated text and shorter: nothing moves"
        usable = max(0.0, gap_after - _GAP_SAFETY_PT)
        if delta_pt <= usable:
            return EXACT, "the surplus fits in the blank that follows (%.0f pt)" % gap_after
        if scale is not None and scale >= _SCALE_FLOOR:
            return FIT, "fixed-width zone: condensed to %.0f %%" % (scale * 100)
        return FIT, "fixed-width zone: condensed to %.0f %%, reduced legibility" % (
            (scale or 1) * 100)

    # Text in the flow of a sentence: pushing it is the natural behaviour.
    if delta_pt <= 0:
        return PUSH, "running text: the line tightens up"
    if room is None or delta_pt <= room:
        return PUSH, "running text: the line grows by %.0f pt, the margin allows it" % delta_pt
    if scale is not None and scale >= _SCALE_FLOOR:
        return FIT, "would run past the margin: condensed to %.0f %%" % (scale * 100)
    return PUSH, "runs past the margin by %.0f pt — condensing would make it illegible" % (
        delta_pt - (room or 0))


# --------------------------------------------------------------------------
# Editable representation of an instruction
# --------------------------------------------------------------------------


@dataclass
class _Item:
    kind: str          # "str" | "num" | "op"
    data: bytes = b""
    value: float = 0.0
    operands: Optional[list] = None
    operator: str = ""
    # Index of the element in the original TJ array. Indispensable: as soon as
    # anything is inserted the positions slide, and a positional index would
    # become wrong for every span left to process.
    orig: int = -1


class _InstrBuilder:
    """
    A text showing instruction, in a form that can be edited.

    It can take interleaved operators ("op"): a stretching Tz has to apply
    between two pieces of text, which a plain TJ array cannot express. On
    emission, the instruction then splits into a sequence.
    """

    def __init__(self, ins):
        self.operator = str(ins.operator)
        self.orig = ins
        self.lead: List[float] = []     # aw/ac operands of the " operator
        self.items: List[_Item] = []
        self.dirty = False

        ops = ins.operands
        if self.operator == "TJ" and ops:
            for e, it in enumerate(list(ops[0])):
                if isinstance(it, pikepdf.String):
                    self.items.append(_Item("str", data=bytes(it), orig=e))
                else:
                    try:
                        self.items.append(_Item("num", value=float(it), orig=e))
                    except Exception:
                        self.items.append(_Item("num", value=0.0, orig=e))
        elif self.operator in ("Tj", "'") and ops:
            self.items.append(_Item("str", data=bytes(ops[0]), orig=0))
        elif self.operator == '"' and len(ops) >= 3:
            self.lead = [float(ops[0]), float(ops[1])]
            self.items.append(_Item("str", data=bytes(ops[2]), orig=0))

    # -- editing ---------------------------------------------------------

    def _str_index(self, elem: int) -> int:
        """elem = index of the element in the original TJ, or -1 for Tj/'/"."""
        if elem < 0:
            for i, it in enumerate(self.items):
                if it.kind == "str":
                    return i
            return -1
        for i, it in enumerate(self.items):
            if it.orig == elem and it.kind == "str":
                return i
        return -1

    def set_string(self, elem: int, data: bytes) -> bool:
        i = self._str_index(elem)
        if i < 0 or self.items[i].kind != "str":
            return False
        if self.items[i].data != data:
            self.items[i].data = data
            self.dirty = True
        return True

    def _insert_at(self, elem: int, code_offset: int, code_len: int,
                   item: _Item) -> bool:
        """
        Inserts an element *inside* a string, cutting it at the requested code.

        Indispensable: a whole line is often a single string, along the lines
        of "Signé le 7 janvier 2019 à Paris". Putting an adjustment after that
        string would come too late — "à Paris" would already have been drawn in
        the wrong place. You have to cut right after the replaced span.
        """
        i = self._str_index(elem)
        if i < 0:
            return False

        data = self.items[i].data
        cut = max(0, code_offset) * max(1, code_len)

        if cut <= 0:
            self.items.insert(i, item)
        elif cut >= len(data):
            self.items.insert(i + 1, item)
        else:
            self.items[i] = _Item("str", data=data[:cut], orig=elem)
            self.items.insert(i + 1, item)
            # The tail is no longer a target: everything left to process in
            # this operator sits before the cut.
            self.items.insert(i + 2, _Item("str", data=data[cut:], orig=-1))
        self.dirty = True
        return True

    def insert_number_at(self, elem: int, code_offset: int, code_len: int,
                         value: float) -> bool:
        if abs(value) < 1e-6:
            return True
        return self._insert_at(elem, code_offset, code_len,
                               _Item("num", value=round(value, 5)))

    def insert_op_at(self, elem: int, code_offset: int, code_len: int,
                     operands: list, operator: str) -> bool:
        return self._insert_at(elem, code_offset, code_len,
                               _Item("op", operands=operands, operator=operator))

    # -- emission --------------------------------------------------------

    def emit(self) -> List[Tuple[list, pikepdf.Operator]]:
        if not self.dirty:
            return [(list(self.orig.operands), self.orig.operator)]

        has_num = any(it.kind == "num" for it in self.items)
        has_op = any(it.kind == "op" for it in self.items)

        out: List[Tuple[list, pikepdf.Operator]] = []

        # ' and " carry a line break with them: we restore it explicitly
        # before switching to a sequence.
        if (has_num or has_op) and self.operator == '"':
            out.append(([self.lead[0]], pikepdf.Operator("Tw")))
            out.append(([self.lead[1]], pikepdf.Operator("Tc")))
            out.append(([], pikepdf.Operator("T*")))
        elif (has_num or has_op) and self.operator == "'":
            out.append(([], pikepdf.Operator("T*")))

        if not has_num and not has_op:
            # no insertion: we can stay on the original operator
            arr = [pikepdf.String(it.data) for it in self.items if it.kind == "str"]
            if not arr:
                arr = [pikepdf.String(b"")]
            if self.operator == "TJ":
                return [([pikepdf.Array(arr)], pikepdf.Operator("TJ"))]
            if self.operator == '"':
                return [([self.lead[0], self.lead[1], arr[0]], pikepdf.Operator('"'))]
            return [([arr[0]], pikepdf.Operator(self.operator))]

        run: List = []

        def flush() -> None:
            if not run:
                return
            if any(isinstance(x, pikepdf.String) and len(bytes(x)) for x in run) \
                    or any(not isinstance(x, pikepdf.String) for x in run):
                out.append(([pikepdf.Array(list(run))], pikepdf.Operator("TJ")))
            run.clear()

        for it in self.items:
            if it.kind == "op":
                flush()
                out.append((list(it.operands or []), pikepdf.Operator(it.operator)))
            elif it.kind == "str":
                run.append(pikepdf.String(it.data))
            else:
                v = it.value
                run.append(int(v) if float(v).is_integer() else round(v, 5))
        flush()

        return out or [([pikepdf.Array([pikepdf.String(b"")])], pikepdf.Operator("TJ"))]


# --------------------------------------------------------------------------
# Splitting a span
# --------------------------------------------------------------------------


@dataclass
class _Span:
    op_index: int
    code_start: int
    code_end: int      # exclusive


@dataclass
class _GroupPlan:
    """A source group paired with a piece of the replacement text."""

    spans: List[_Span]
    word: str
    aligned: bool
    raw_old: float = 0.0     # advance without the Th factor, in points
    raw_new: float = 0.0
    scale: Optional[float] = None   # new Th (in %) to apply, or None
    mode: str = EXACT               # exact | fit | push
    reason: str = ""


def _resolve_span(unit: ContentUnit, start: int, end: int
                  ) -> Tuple[Optional[List[List[_Span]]], Optional[str]]:
    """
    Converts a text range into *groups* of code ranges.

    A group = a run of characters actually drawn. Groups are separated by
    "synthetic" spaces, that is, blanks obtained by kerning rather than by a
    space glyph. This is the normal case for PDFs produced by TeX and by many
    layout engines: the embedded font then simply contains no space glyph at
    all, and writing "3 January 2021" as one block is impossible.
    """
    if start < 0 or end > len(unit.char_refs) or start >= end:
        return None, "range outside the text of the unit"

    groups: List[List[_Span]] = []
    current: List[_Span] = []

    for i in range(start, end):
        ref = unit.char_refs[i]
        if ref is None:
            # Space obtained by kerning, or a layout break (end of line,
            # change of column). Either way it is a group boundary: each word
            # will be rewritten in its own place, which is what allows
            # replacing a text broken by a line return.
            if current:
                groups.append(current)
                current = []
            continue
        if ref.is_partial:
            return None, "the span crosses an indivisible ligature"
        if current and current[-1].op_index == ref.op_index \
                and current[-1].code_end == ref.code_index:
            current[-1].code_end = ref.code_index + 1
        elif current and current[-1].op_index == ref.op_index \
                and current[-1].code_end == ref.code_index + 1:
            continue  # same code (character already covered)
        else:
            current.append(_Span(ref.op_index, ref.code_index, ref.code_index + 1))

    if current:
        groups.append(current)
    if not groups:
        return None, "no real character in the span"
    return groups, None


def _flatten(groups: List[List[_Span]]) -> List[_Span]:
    return [s for g in groups for s in g]


def _display_missing(chars: Sequence[str]) -> str:
    """A missing space must read as such, not as a blank in the message."""
    out = []
    for c in chars:
        if c == " ":
            out.append("space")
        elif c.isspace():
            out.append("blank U+%04X" % ord(c))
        else:
            out.append(c)
    return ", ".join(out)


def _codes_text(op: ShowOp, a: int, b: int) -> str:
    return "".join(op.texts[a:b])


def _codes_width(op: ShowOp, a: int, b: int) -> float:
    return sum(op.font.width_of_code(c) for c in op.codes[a:b])


def _spaces_count(op: ShowOp, a: int, b: int) -> int:
    if op.font.code_len != 1:
        return 0
    return sum(1 for c in op.codes[a:b] if c == 32)


def _raw_advance(op: ShowOp, width_1000: float, n_chars: int, n_spaces: int) -> float:
    """Advance in points, *without* the horizontal scale factor Th."""
    return width_1000 / 1000.0 * op.fsize + n_chars * op.tc + n_spaces * op.tw


def _splice(data: bytes, code_len: int, a: int, b: int, new: bytes) -> bytes:
    return data[: a * code_len] + new + data[b * code_len :]


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------


def _plan_groups(unit: ContentUnit, groups: List[List[_Span]], new_text: str,
                 fit: str, gap_after: Optional[float] = None,
                 room: Optional[float] = None,
                 allow_missing: bool = False) -> Optional[List[_GroupPlan]]:
    """
    Pairs each source group with a piece of the replacement, decides the
    strategy and the width handling. Modifies nothing.
    """
    if len(groups) == 1:
        words = [new_text]
    else:
        words = new_text.split(" ")
        if len(words) != len(groups):
            compact = [w for w in words if w != ""]
            if len(compact) != len(groups):
                return None
            words = compact

    out: List[_GroupPlan] = []
    for spans, word in zip(groups, words):
        first = unit.ops[spans[0].op_index]

        # original advance
        raw_old = 0.0
        n_old = 0
        for s in spans:
            op = unit.ops[s.op_index]
            n = s.code_end - s.code_start
            n_old += n
            raw_old += _raw_advance(op, _codes_width(op, s.code_start, s.code_end),
                                    n, _spaces_count(op, s.code_start, s.code_end))

        # target advance, written in the font of the group's first operator
        w_new = first.font.width_of_text(word)
        if w_new is None:
            if not allow_missing:
                return None
            w_new = first.font.width_of_text_estimated(word)
        nsp = word.count(" ") if first.font.code_len == 1 else 0
        raw_new = _raw_advance(first, w_new, len(word), nsp)

        delta_pt = (raw_new - raw_old) * first.th / 100.0

        # Can we stay strictly in place? It takes the same number of codes, no
        # composed character, an encoding possible piece by piece, and a
        # negligible width difference.
        aligned = (
            len(word) == n_old
            and abs(delta_pt) < _NEGLIGIBLE_PT
            and all(len(unit.ops[s.op_index].texts[i]) == 1
                    for s in spans for i in range(s.code_start, s.code_end))
        )
        if aligned:
            pos = 0
            for s in spans:
                op = unit.ops[s.op_index]
                n = s.code_end - s.code_start
                if op.font.encode_text(word[pos : pos + n]) is None:
                    aligned = False
                    break
                pos += n

        if not aligned and first.font.encode_text(word) is None and not allow_missing:
            return None

        plan = _GroupPlan(spans=spans, word=word, aligned=aligned,
                          raw_old=raw_old, raw_new=raw_new)

        if aligned:
            plan.mode = EXACT
            plan.reason = "in-place replacement"
            out.append(plan)
            continue

        # The scale it would take to fit within the original width.
        ratio = (raw_old / raw_new) if raw_new > 1e-6 else None
        # 6 decimals: rounding the scale to 3 left a drift of a few thousandths
        # of a point, invisible to the eye but enough to shift an antialiasing
        # pixel and make a strict check fail.
        new_th = round(first.th * ratio, 6) if (ratio and first.fsize) else None

        mode, reason = (fit, "") if fit in (EXACT, FIT, PUSH) else _decide_fit(
            delta_pt, ratio, gap_after, room, first.fsize)
        if not reason:
            reason = {EXACT: "compensation forced",
                      FIT: "fit forced",
                      PUSH: "natural flow forced"}.get(mode, "")

        if mode == FIT and new_th is None:
            mode, reason = EXACT, "fit impossible (degenerate graphics state)"

        plan.mode = mode
        plan.reason = reason
        plan.scale = new_th if mode == FIT else None
        out.append(plan)

    return out


def plan_replacement(unit: ContentUnit, rep: Replacement) -> ReplacementResult:
    """Assesses whether a replacement is feasible, without modifying anything."""
    res = ReplacementResult(
        ident=rep.ident, unit_id=unit.unit_id,
        old_text=unit.text[rep.start : rep.end], new_text=rep.new_text,
        strategy=REFUS, ok=False,
        shared_xobject=unit.instance_count,
    )

    groups, err = _resolve_span(unit, rep.start, rep.end)
    if groups is None:
        res.reason = err
        return res

    spans = _flatten(groups)
    ops = [unit.ops[s.op_index] for s in spans]
    res.font_names = sorted({o.font.display_name for o in ops})
    res.invisible_text = all(o.tmode == 3 for o in ops)

    primary = ops[0].font
    if not primary.editable:
        res.reason = primary.not_editable_reason or "font not editable"
        return res
    if ops[0].fsize == 0:
        res.reason = "font size is zero (degenerate graphics state)"
        return res

    # We plan while tolerating missing glyphs: even if the replacement ends up
    # refused for want of a glyph, the user must see the width it will take
    # once the font is repaired.
    plan = _plan_groups(unit, groups, rep.new_text, rep.fit,
                        rep.gap_after, rep.room, allow_missing=True)

    # What we really need to be able to write depends on the splitting: word by
    # word, spaces are never drawn, so their absence from the font is not an
    # obstacle. We judge coverage only on what we are actually going to write.
    to_write = "".join(g.word for g in plan) if plan else rep.new_text
    level, missing = primary.coverage_of_text(to_write)
    res.coverage = level
    res.missing_glyphs = missing
    if plan:
        _fill_measures(res, unit, plan, rep)

    if level in (ABSENT, INCERTAIN):
        if level == ABSENT:
            res.reason = "glyphs missing from the embedded font \"%s\": %s" % (
                primary.display_name, _display_missing(missing))
        else:
            # The font declares these characters but we could not verify that
            # it draws them. Writing anyway means risking the silent blank box
            # — precisely what this tool promises to avoid.
            res.reason = ("cannot verify that \"%s\" draws %s "
                          "(font program not inspectable)" % (
                              primary.display_name, _display_missing(missing)))
        res.ok = False
        res.strategy = REFUS
        return res

    if plan is None:
        res.reason = ("the replacement does not have the same number of words "
                      "as the source, which separates its words by kerning "
                      "and not by a space glyph")
        return res

    res.strategy = ALIGNE if all(g.aligned for g in plan) else (
        MOTS if len(groups) > 1 else REGROUPE)
    res.ok = True
    return res


def _fill_measures(res: ReplacementResult, unit: ContentUnit,
                   plan: List[_GroupPlan], rep: Replacement) -> None:
    """Carries everything that concerns width over into the result."""
    res.width_old = round(sum(g.raw_old * unit.ops[g.spans[0].op_index].th / 100.0
                              for g in plan), 3)
    res.width_new = round(sum(g.raw_new * unit.ops[g.spans[0].op_index].th / 100.0
                              for g in plan), 3)
    res.width_delta = round(res.width_new - res.width_old, 3)

    # The mode kept is that of the group that matters most: the one whose
    # width difference is the largest.
    dominant = max(plan, key=lambda g: abs(g.raw_new - g.raw_old))
    res.fit_used = dominant.mode
    res.fit_reason = dominant.reason

    scales = [g.scale for g in plan if g.scale is not None]
    if scales:
        base = unit.ops[plan[0].spans[0].op_index].th or 100.0
        res.scaled = round(sum(scales) / len(scales) / base, 4)

    res.reflow = any(g.mode == PUSH and abs(g.raw_new - g.raw_old) >= _NEGLIGIBLE_PT
                     for g in plan)
    res.overlap_risk = any(g.mode == EXACT and (g.raw_new - g.raw_old) > _NEGLIGIBLE_PT
                           and not g.aligned for g in plan)
    res.compensated = not all(g.aligned for g in plan) or abs(res.width_delta) >= 0.001


# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------


def apply_replacements(pdf: pikepdf.Pdf, unit: ContentUnit,
                       reps: Sequence[Replacement]
                       ) -> Tuple[List[ReplacementResult], bool]:
    """
    Applies the replacements of one unit and rewrites its stream.
    Returns (results, stream actually modified).
    """
    results: List[ReplacementResult] = []
    jobs: List[Tuple[Replacement, ReplacementResult, List[_GroupPlan]]] = []

    for rep in reps:
        res = plan_replacement(unit, rep)
        results.append(res)
        if not res.ok:
            continue
        groups, _err = _resolve_span(unit, rep.start, rep.end)
        if groups is None:
            continue
        gplan = _plan_groups(unit, groups, rep.new_text, rep.fit,
                        rep.gap_after, rep.room)
        if gplan:
            jobs.append((rep, res, gplan))

    if not jobs:
        return results, False

    # From the last span to the first: every insertion then happens downstream
    # of what is left to write, and the code indices stay valid.
    jobs.sort(key=lambda p: p[0].start, reverse=True)

    builders: Dict[int, _InstrBuilder] = {}

    def builder_for(instr_idx: int) -> _InstrBuilder:
        b = builders.get(instr_idx)
        if b is None:
            b = _InstrBuilder(unit.instructions[instr_idx])
            builders[instr_idx] = b
        return b

    changed = False
    for rep, res, gplan in jobs:
        try:
            for g in reversed(gplan):
                if g.aligned:
                    changed |= _write_aligned(unit, rep, g, builder_for)
                else:
                    changed |= _write_block(unit, rep, g, builder_for)
        except Exception as e:
            res.ok = False
            res.strategy = REFUS
            res.reason = "write failure: %s: %s" % (type(e).__name__, e)

    if not changed:
        return results, False

    out: List = []
    for idx, ins in enumerate(unit.instructions):
        b = builders.get(idx)
        if b is None or not b.dirty:
            out.append(ins)
        else:
            out.extend(b.emit())

    data = pikepdf.unparse_content_stream(out)
    _write_stream(pdf, unit, data)
    return results, True


def _write_aligned(unit: ContentUnit, rep: Replacement, g: _GroupPlan,
                   builder_for) -> bool:
    """In-place replacement, code by code, moving nothing."""
    chunks: List[Tuple[_Span, str]] = []
    pos = 0
    for s in g.spans:
        n = s.code_end - s.code_start
        chunks.append((s, g.word[pos : pos + n]))
        pos += n

    changed = False
    for s, chunk in reversed(chunks):
        op = unit.ops[s.op_index]
        if chunk == _codes_text(op, s.code_start, s.code_end):
            continue
        new_bytes = op.font.encode_text(chunk)
        if new_bytes is None:
            raise ValueError("encoding impossible for \"%s\"" % chunk)

        b = builder_for(op.instr)
        i = b._str_index(op.elem)
        if i < 0:
            raise ValueError("operand not found")
        b.set_string(op.elem, _splice(b.items[i].data, op.font.code_len,
                                      s.code_start, s.code_end, new_bytes))
        changed = True

        # Leftover width inside this operator (font rounding).
        w_old = _codes_width(op, s.code_start, s.code_end)
        w_new = op.font.width_of_text(chunk)
        if w_new is None:
            continue
        sp_old = _spaces_count(op, s.code_start, s.code_end)
        sp_new = chunk.count(" ") if op.font.code_len == 1 else 0
        n_comp = (w_new - w_old)
        if op.fsize:
            n_comp += 1000.0 / op.fsize * ((sp_new - sp_old) * op.tw)
        if abs(n_comp) > 0.01:
            _insert_comp(b, op.elem, op.font.code_len, s.code_start,
                         s.code_start + len(chunk), n_comp, rep.anchor)
    return changed


def _write_block(unit: ContentUnit, rep: Replacement, g: _GroupPlan,
                 builder_for) -> bool:
    """Rewriting a group as a single block, with width catch-up."""
    first_op = unit.ops[g.spans[0].op_index]
    font = first_op.font
    new_bytes = font.encode_text(g.word)
    if new_bytes is None:
        raise ValueError("encoding impossible")

    # 1. all the new text goes into the first operator of the group
    b0 = builder_for(first_op.instr)
    i0 = b0._str_index(first_op.elem)
    if i0 < 0:
        raise ValueError("operand not found")
    s0 = g.spans[0]
    b0.set_string(first_op.elem,
                  _splice(b0.items[i0].data, font.code_len,
                          s0.code_start, s0.code_end, new_bytes))

    # 2. the following operators of the group are emptied of their portion
    for s in g.spans[1:]:
        op = unit.ops[s.op_index]
        b = builder_for(op.instr)
        i = b._str_index(op.elem)
        if i < 0:
            raise ValueError("operand not found")
        b.set_string(op.elem, _splice(b.items[i].data, op.font.code_len,
                                      s.code_start, s.code_end, b""))

    start_off = s0.code_start
    end_off = s0.code_start + len(g.word)
    code_len = font.code_len

    if g.mode == PUSH:
        return True   # natural width owned, the line readjusts

    # 3a. horizontal stretch: the new text is framed by two Tz. They have to be
    #     inserted from the latest to the earliest so that the first cut point
    #     stays valid.
    if g.mode == FIT and g.scale is not None:
        b0.insert_op_at(first_op.elem, end_off, code_len,
                        [_num(first_op.th)], "Tz")
        b0.insert_op_at(first_op.elem, start_off, code_len,
                        [_num(g.scale)], "Tz")
        residual = (g.raw_old * first_op.th / 100.0
                    - g.raw_new * g.scale / 100.0)
        if abs(residual) > 1e-7 and first_op.fsize and first_op.th:
            n = -residual * 100000.0 / (first_op.fsize * first_op.th)
            _insert_comp(b0, first_op.elem, code_len, start_off, end_off,
                         n, rep.anchor)
        return True

    # 3b. compensation by kerning
    n_comp = (g.raw_new - g.raw_old) * 1000.0 / first_op.fsize if first_op.fsize else 0.0
    if abs(n_comp) > 0.01:
        _insert_comp(b0, first_op.elem, code_len, start_off, end_off,
                     n_comp, rep.anchor)
    return True


def _num(v: float):
    return int(v) if float(v).is_integer() else round(v, 6)


def _insert_comp(builder: _InstrBuilder, elem: int, code_len: int,
                 start_off: int, end_off: int, n_comp: float, anchor: str) -> None:
    """
    n_comp > 0 means "the new one is wider": the TJ number pulls back to the
    left so that the rest of the line does not move.
      left   -> all after    (the text stays pinned left, the slack is on the right)
      right  -> all before   (the text stays pinned right)
      center -> half/half
    We insert from the end towards the start so that the first cut point stays
    valid after the second insertion.
    """
    if anchor == "right":
        builder.insert_number_at(elem, start_off, code_len, n_comp)
    elif anchor == "center":
        builder.insert_number_at(elem, end_off, code_len, n_comp / 2.0)
        builder.insert_number_at(elem, start_off, code_len, n_comp / 2.0)
    else:
        builder.insert_number_at(elem, end_off, code_len, n_comp)


def _write_stream(pdf: pikepdf.Pdf, unit: ContentUnit, data: bytes) -> None:
    obj = unit.stream_obj
    if unit.kind == "page":
        obj.Contents = pdf.make_stream(data)
    else:
        obj.write(data)
