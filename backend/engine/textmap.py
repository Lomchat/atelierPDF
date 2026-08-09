"""
Decoding a PDF content stream into text, with full traceability.

This is where the difference is made between "extracting text" (easy, everyone
knows how) and "knowing exactly where that text is written in the file"
(required to rewrite it without breaking anything).

For every character produced we keep:
  - the content unit (page stream or form XObject)
  - the index of the instruction in that stream
  - the index of the element in the TJ array
  - the index of the code byte inside the string
  - the graphics state in force (font, size, char spacing, word spacing, scale)

Without that traceability chain, an "exact" replacement is impossible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pikepdf

from .fonts import FontInfo, analyze_font

# Operators that move the text cursor: we cut the string there so that a date
# cannot match across two lines or two columns.
_POSITIONING = {"Td", "TD", "Tm", "T*", "BT", "ET"}

# A TJ adjustment above this threshold (in thousandths of an em) counts as real
# spacing, not kerning: we cut.
_KERN_BREAK = 220.0


# --------------------------------------------------------------------------
# PDF matrix algebra (a b c d e f), point transformed by p × M
# --------------------------------------------------------------------------

Matrix = Tuple[float, float, float, float, float, float]
IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def mat_mul(m: Matrix, n: Matrix) -> Matrix:
    a1, b1, c1, d1, e1, f1 = m
    a2, b2, c2, d2, e2, f2 = n
    return (a1 * a2 + b1 * c2,
            a1 * b2 + b1 * d2,
            c1 * a2 + d1 * c2,
            c1 * b2 + d1 * d2,
            e1 * a2 + f1 * c2 + e2,
            e1 * b2 + f1 * d2 + f2)


def mat_apply(m: Matrix, x: float, y: float) -> Tuple[float, float]:
    a, b, c, d, e, f = m
    return (x * a + y * c + e, x * b + y * d + f)


def translate(tx: float, ty: float) -> Matrix:
    return (1.0, 0.0, 0.0, 1.0, tx, ty)


@dataclass
class TextState:
    font_key: Optional[str] = None
    font: Optional[FontInfo] = None
    fsize: float = 0.0
    tc: float = 0.0     # char spacing
    tw: float = 0.0     # word spacing
    th: float = 100.0   # horizontal scale (%)
    trise: float = 0.0
    tmode: int = 0      # render mode (3 = invisible)
    tl: float = 0.0     # leading

    def copy(self) -> "TextState":
        return TextState(self.font_key, self.font, self.fsize, self.tc,
                         self.tw, self.th, self.trise, self.tmode, self.tl)


@dataclass
class ShowOp:
    """A byte string that is actually drawn, and where it is drawn."""

    unit_id: str
    instr: int
    elem: int              # index in the TJ array; -1 if a direct operand
    data: bytes
    font: FontInfo
    fsize: float
    tc: float
    tw: float
    th: float
    tmode: int
    trise: float = 0.0
    ctm: Matrix = IDENTITY       # current matrix at draw time
    tm: Matrix = IDENTITY        # text matrix at the start of the operator
    codes: List[int] = field(default_factory=list)
    texts: List[str] = field(default_factory=list)  # unicode per code

    @property
    def text(self) -> str:
        return "".join(self.texts)

    def code_advance(self, i: int) -> float:
        """Advance of one code in text units, Th factor included."""
        c = self.codes[i]
        w = self.font.width_of_code(c) / 1000.0 * self.fsize + self.tc
        if c == 32 and self.font.code_len == 1:
            w += self.tw
        return w * self.th / 100.0

    def offset_at(self, i: int) -> float:
        """Horizontal offset, in text units, before the code at index i."""
        return sum(self.code_advance(k) for k in range(min(i, len(self.codes))))

    def total_advance(self) -> float:
        return self.offset_at(len(self.codes))

    def quad(self, i0: int, i1: int, extra: float = 0.0) -> List[Tuple[float, float]]:
        """
        The four corners of the box of codes [i0, i1), in PDF user space. This
        is the exact computation: we compose the text rendering matrix with the
        current matrix, exactly the way the renderer does.
        """
        x0 = self.offset_at(i0)
        x1 = self.offset_at(i1) + max(0.0, extra)
        asc = self.fsize * (self.font.ascent / 1000.0) + self.trise
        desc = self.fsize * (self.font.descent / 1000.0) + self.trise
        base = mat_mul(self.tm, self.ctm)
        return [mat_apply(base, x0, desc), mat_apply(base, x1, desc),
                mat_apply(base, x1, asc), mat_apply(base, x0, asc)]

    def bbox(self, i0: int, i1: int, extra: float = 0.0
             ) -> Tuple[float, float, float, float]:
        pts = self.quad(i0, i1, extra)
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return (min(xs), min(ys), max(xs), max(ys))


@dataclass
class CharRef:
    """One character of the reconstructed text, and its exact origin."""

    op_index: int      # index in ContentUnit.ops
    code_index: int    # index of the code inside that op
    is_partial: bool   # True if this code produces several characters (ligature)


@dataclass
class ContentUnit:
    """A parsed content stream: page or form XObject."""

    unit_id: str
    kind: str                       # "page" | "xobject"
    page_number: int
    instructions: List = field(default_factory=list)
    ops: List[ShowOp] = field(default_factory=list)
    text: str = ""
    char_refs: List[Optional[CharRef]] = field(default_factory=list)
    stream_obj = None               # pikepdf object to rewrite
    instance_count: int = 1         # number of times the unit is drawn
    parse_error: Optional[str] = None


def _to_bytes(obj) -> bytes:
    try:
        if isinstance(obj, pikepdf.String):
            return bytes(obj)
        return bytes(obj)
    except Exception:
        return b""


def _f(obj, default=0.0) -> float:
    try:
        return float(obj)
    except Exception:
        return default


class ContentWalker:
    """Walks the streams of a page and extracts traceable text from them."""

    def __init__(self, pdf: pikepdf.Pdf, font_cache: Dict[str, FontInfo]):
        self.pdf = pdf
        self.font_cache = font_cache
        self.units: Dict[str, ContentUnit] = {}
        self._seen_xobjects: Dict[str, int] = {}

    # -- font resolution -----------------------------------------------------

    def _resolve_font(self, resources, name: str) -> Optional[FontInfo]:
        if resources is None:
            return None
        try:
            fonts = resources.get("/Font")
            if fonts is None:
                return None
            obj = fonts.get("/" + name if not name.startswith("/") else name)
            if obj is None:
                return None
            return analyze_font(name.lstrip("/"), obj, self.font_cache)
        except Exception:
            return None

    # -- traversal -----------------------------------------------------------

    def walk_page(self, page, page_number: int) -> List[ContentUnit]:
        units: List[ContentUnit] = []
        try:
            unit_id = "p%d" % page_number
            instrs = list(pikepdf.parse_content_stream(page))
            unit = ContentUnit(unit_id=unit_id, kind="page", page_number=page_number,
                               instructions=instrs)
            unit.stream_obj = page
            self.units[unit_id] = unit
            units.append(unit)
            resources = page.get("/Resources")
            self._scan(unit, instrs, resources, page_number, units, depth=0)
            self._finalize(unit)
        except Exception as e:
            u = ContentUnit(unit_id="p%d" % page_number, kind="page",
                            page_number=page_number)
            u.parse_error = "%s: %s" % (type(e).__name__, e)
            units.append(u)
        return units

    def _scan(self, unit: ContentUnit, instrs, resources, page_number: int,
              units: List[ContentUnit], depth: int, base_ctm: Matrix = IDENTITY) -> None:
        gs_stack: List[Tuple[TextState, Matrix]] = []
        ts = TextState()
        ctm: Matrix = base_ctm
        tm: Matrix = IDENTITY      # text matrix
        tlm: Matrix = IDENTITY     # line-start matrix

        for idx, ins in enumerate(instrs):
            op = str(ins.operator)
            ops = ins.operands

            if op == "q":
                gs_stack.append((ts.copy(), ctm))
                continue
            if op == "Q":
                if gs_stack:
                    ts, ctm = gs_stack.pop()
                    ts = ts.copy()
                else:
                    ts, ctm = TextState(), base_ctm
                continue

            if op == "cm" and len(ops) >= 6:
                ctm = mat_mul(tuple(_f(x) for x in ops[:6]), ctm)
                continue

            # --- text positioning ----------------------------------------
            if op == "BT":
                tm = tlm = IDENTITY
                self._append_break(unit)
                continue
            if op == "ET":
                self._append_break(unit)
                continue
            if op == "Tm" and len(ops) >= 6:
                tm = tlm = tuple(_f(x) for x in ops[:6])
                self._append_break(unit)
                continue
            if op == "Td" and len(ops) >= 2:
                tlm = mat_mul(translate(_f(ops[0]), _f(ops[1])), tlm)
                tm = tlm
                self._append_break(unit)
                continue
            if op == "TD" and len(ops) >= 2:
                ts.tl = -_f(ops[1])
                tlm = mat_mul(translate(_f(ops[0]), _f(ops[1])), tlm)
                tm = tlm
                self._append_break(unit)
                continue
            if op == "T*":
                tlm = mat_mul(translate(0.0, -ts.tl), tlm)
                tm = tlm
                self._append_break(unit)
                continue
            if op == "TL" and ops:
                ts.tl = _f(ops[0])
                continue

            if op == "Tf" and len(ops) >= 2:
                name = str(ops[0]).lstrip("/")
                ts.font_key = name
                ts.font = self._resolve_font(resources, name)
                ts.fsize = _f(ops[1])
                continue
            if op == "Tc" and ops:
                ts.tc = _f(ops[0]); continue
            if op == "Tw" and ops:
                ts.tw = _f(ops[0]); continue
            if op == "Tz" and ops:
                ts.th = _f(ops[0], 100.0); continue
            if op == "Ts" and ops:
                ts.trise = _f(ops[0]); continue
            if op == "Tr" and ops:
                ts.tmode = int(_f(ops[0])); continue
            if op == "gs" and ops:
                # an ExtGState can carry /Font [ref size]
                try:
                    eg = resources.get("/ExtGState").get(str(ops[0]))
                    fnt = eg.get("/Font") if eg is not None else None
                    if fnt is not None and len(fnt) >= 2:
                        ts.font = analyze_font("gs", fnt[0], self.font_cache)
                        ts.fsize = _f(fnt[1])
                except Exception:
                    pass
                continue

            # --- text showing operators ------------------------------------
            if op == "Tj" and ops:
                tm = self._emit(unit, idx, -1, _to_bytes(ops[0]), ts, ctm, tm)
                continue

            if op == "'" and ops:
                tlm = mat_mul(translate(0.0, -ts.tl), tlm)
                tm = tlm
                self._append_break(unit)
                tm = self._emit(unit, idx, -1, _to_bytes(ops[0]), ts, ctm, tm)
                continue

            if op == '"' and len(ops) >= 3:
                ts.tw = _f(ops[0])
                ts.tc = _f(ops[1])
                tlm = mat_mul(translate(0.0, -ts.tl), tlm)
                tm = tlm
                self._append_break(unit)
                tm = self._emit(unit, idx, -1, _to_bytes(ops[2]), ts, ctm, tm)
                continue

            if op == "TJ" and ops:
                try:
                    arr = list(ops[0])
                except Exception:
                    continue
                for e, item in enumerate(arr):
                    if isinstance(item, pikepdf.String):
                        tm = self._emit(unit, idx, e, _to_bytes(item), ts, ctm, tm)
                    else:
                        adj = _f(item)
                        # A TJ number moves the cursor: the text matrix has to
                        # account for it, otherwise every later position on
                        # the line is wrong.
                        tm = mat_mul(
                            translate(-adj / 1000.0 * ts.fsize * ts.th / 100.0, 0.0),
                            tm)
                        if abs(adj) >= _KERN_BREAK:
                            # Many producers write spaces as kerning rather
                            # than with a real space character. We materialise
                            # a "synthetic" space (with no character
                            # reference) so that "15 mars 2024" stays
                            # detectable, knowing full well it will force a
                            # recomposed rewrite.
                            self._append_gap(unit)
                continue

            # --- form XObjects ----------------------------------------------
            if op == "Do" and ops and depth < 6:
                self._descend_xobject(str(ops[0]).lstrip("/"), resources,
                                      page_number, units, depth, ctm)
                continue

    def _descend_xobject(self, name, resources, page_number, units, depth,
                         ctm: Matrix = IDENTITY):
        try:
            xobjs = resources.get("/XObject")
            if xobjs is None:
                return
            xo = xobjs.get("/" + name)
            if xo is None:
                return
            if str(xo.get("/Subtype") or "") != "/Form":
                return
            xid = "x%d_%d" % xo.objgen
        except Exception:
            return

        self._seen_xobjects[xid] = self._seen_xobjects.get(xid, 0) + 1
        if xid in self.units:
            self.units[xid].instance_count = self._seen_xobjects[xid]
            return

        try:
            sub_instrs = list(pikepdf.parse_content_stream(xo))
        except Exception as e:
            u = ContentUnit(unit_id=xid, kind="xobject", page_number=page_number)
            u.parse_error = str(e)
            self.units[xid] = u
            units.append(u)
            return

        sub = ContentUnit(unit_id=xid, kind="xobject", page_number=page_number,
                          instructions=sub_instrs)
        sub.stream_obj = xo
        self.units[xid] = sub
        units.append(sub)
        sub_res = xo.get("/Resources") or resources
        # An XObject carries its own matrix, composed with the context's.
        try:
            mx = xo.get("/Matrix")
            form_m = tuple(_f(v) for v in list(mx)[:6]) if mx is not None else IDENTITY
        except Exception:
            form_m = IDENTITY
        self._scan(sub, sub_instrs, sub_res, page_number, units, depth + 1,
                   mat_mul(form_m, ctm))
        self._finalize(sub)

    # -- building the text --------------------------------------------------

    def _append_break(self, unit: ContentUnit) -> None:
        """Hard break: no date can span it."""
        if unit.text and not unit.text.endswith("\n"):
            if unit.text.endswith(" ") and unit.char_refs[-1] is None:
                unit.text = unit.text[:-1]
                unit.char_refs.pop()
            unit.text += "\n"
            unit.char_refs.append(None)

    def _append_gap(self, unit: ContentUnit) -> None:
        """Synthetic space coming from kerning: it can be crossed, but it
        forces a recomposed rewrite (you cannot write "inside" a blank that is
        not a character)."""
        if unit.text and unit.text[-1] not in (" ", "\n"):
            unit.text += " "
            unit.char_refs.append(None)

    def _emit(self, unit: ContentUnit, instr: int, elem: int, data: bytes,
              ts: TextState, ctm: Matrix, tm: Matrix) -> Matrix:
        if not data or ts.font is None:
            return tm
        decoded = ts.font.decode(data)
        sop = ShowOp(
            unit_id=unit.unit_id, instr=instr, elem=elem, data=data,
            font=ts.font, fsize=ts.fsize, tc=ts.tc, tw=ts.tw, th=ts.th,
            tmode=ts.tmode, trise=ts.trise, ctm=ctm, tm=tm,
            codes=[c for c, _ in decoded], texts=[t for _, t in decoded],
        )
        op_index = len(unit.ops)
        unit.ops.append(sop)
        ts.font.used_codes.update(sop.codes)

        for ci, txt in enumerate(sop.texts):
            if not txt:
                continue
            partial = len(txt) > 1
            for ch in txt:
                unit.text += ch
                unit.char_refs.append(CharRef(op_index, ci, partial))

        return mat_mul(translate(sop.total_advance(), 0.0), tm)

    def _finalize(self, unit: ContentUnit) -> None:
        assert len(unit.text) == len(unit.char_refs), (
            "text/reference desynchronisation in %s" % unit.unit_id
        )


def build_units(pdf: pikepdf.Pdf, page_numbers: Optional[List[int]] = None):
    """Parses every requested page. Returns (units, font_cache)."""
    font_cache: Dict[str, FontInfo] = {}
    walker = ContentWalker(pdf, font_cache)
    all_units: List[ContentUnit] = []
    n = len(pdf.pages)
    targets = range(n) if page_numbers is None else page_numbers
    for i in targets:
        if 0 <= i < n:
            all_units.extend(walker.walk_page(pdf.pages[i], i))
    return all_units, font_cache
