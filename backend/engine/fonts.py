"""
Analysis of a PDF font: encoding, advance widths, real glyph coverage.

The guiding idea: never *assume* a glyph exists. A PDF almost always embeds a
subset of the font ("ABCDEF+Helvetica") containing only the glyphs actually
drawn. Writing a "7" into a font that never needed a "7" produces a blank box —
silently.

So for every font we build a coverage verdict with five levels of confidence:

  CERTAIN   the code is already drawn elsewhere in the document -> 100% safe
  PROGRAMME the glyph exists in the embedded font file          -> safe
  DECLARE   the font declares it and is not embedded: the viewer
            will use its own, complete font                     -> safe in practice
  INCERTAIN the font is embedded but its program is unreadable
            (Type1, CFF). It *declares* the character, but a subset
            often declares more than it draws                   -> to repair
  ABSENT    no trace at all                                     -> refusal

The INCERTAIN level exists because the optimistic bet is expensive: writing a
character you believe is there when it is not produces a blank box, silently,
in a tool whose whole promise is to break nothing.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import pikepdf

from .cmap import CMap, build_simple_encoding, parse_tounicode
from .encodings import STANDARD_14
from .glyphlist import glyph_name_to_unicode

# Confidence levels, from best to worst.
CERTAIN = "certain"
PROGRAMME = "program"
DECLARE = "declare"
INCERTAIN = "unverified"
ABSENT = "absent"

_CONFIDENCE_ORDER = {CERTAIN: 0, PROGRAMME: 1, DECLARE: 2, INCERTAIN: 3, ABSENT: 4}

_SUBSET_PREFIX_RE = re.compile(r"^([A-Z]{6})\+(.*)$")


def _s(obj) -> Optional[str]:
    """pikepdf Name/String -> python str."""
    if obj is None:
        return None
    try:
        if isinstance(obj, pikepdf.Name):
            return str(obj).lstrip("/")
        if isinstance(obj, pikepdf.String):
            return str(obj)
        return str(obj)
    except Exception:
        return None


def _num(obj, default=None):
    try:
        return float(obj)
    except Exception:
        return default


@dataclass
class FontInfo:
    """Everything we know about a font, from the "can I write in it?" angle."""

    key: str                       # resource name, e.g. "F3"
    objgen: str                    # identity of the PDF object, to deduplicate
    subtype: str = ""
    base_font: str = ""
    display_name: str = ""         # name without the subset prefix
    subset_prefix: Optional[str] = None
    embedded: bool = False
    font_file_key: Optional[str] = None
    symbolic: bool = False

    code_len: int = 1              # 1 byte (simple font) or 2 (Identity-H)
    encoding_name: Optional[str] = None
    cmap_supported: bool = True    # False = exotic predefined CMap

    to_unicode: Dict[int, str] = field(default_factory=dict)
    from_unicode: Dict[str, List[int]] = field(default_factory=dict)

    widths: Dict[int, float] = field(default_factory=dict)   # code -> 1/1000 em
    default_width: float = 0.0

    # Codes actually drawn in the document (filled in by the extraction).
    used_codes: Set[int] = field(default_factory=set)
    # Glyphs present in the embedded font file (by code).
    program_codes: Optional[Set[int]] = None
    program_error: Optional[str] = None
    # Codes we just injected ourselves: we know they exist, even when the font
    # program remains otherwise inspectable.
    repaired_codes: Set[int] = field(default_factory=set)
    # Explicit CID -> GID table, when the composite font is not in Identity.
    # Two bytes per CID. Without it, writing a fresh CID leads nowhere: the
    # viewer reads a zero there, so .notdef.
    cid_to_gid: Optional[bytes] = None

    editable: bool = True
    not_editable_reason: Optional[str] = None

    # Vertical metrics, in thousandths of an em. Used to compute the box of a
    # text: without them we fall back on an approximation.
    ascent: float = 780.0
    descent: float = -220.0

    # ------------------------------------------------------------------
    # Advance widths
    # ------------------------------------------------------------------

    def width_of_code(self, code: int) -> float:
        w = self.widths.get(code)
        return self.default_width if w is None else w

    def width_of_text(self, text: str) -> Optional[float]:
        """Total advance width of a string, in thousandths of an em. None if a
        character is not encodable."""
        total = 0.0
        for ch in text:
            code = self.encode_char(ch)
            if code is None:
                return None
            total += self.width_of_code(code)
        return total

    def width_of_text_estimated(self, text: str) -> float:
        """
        Approximate advance width, including for characters the font cannot
        write yet. Used by the simulation: a missing glyph will be injected
        back at apply time, and the user needs to see right now whether the
        replacement will fit in the room available.
        """
        known = [w for w in self.widths.values() if w > 0]
        fallback = self.default_width or (sum(known) / len(known) if known else 500.0)
        total = 0.0
        for ch in text:
            code = self.encode_char(ch)
            total += self.width_of_code(code) if code is not None else fallback
        return total

    @property
    def digit_widths(self) -> Dict[str, Optional[float]]:
        out: Dict[str, Optional[float]] = {}
        for d in "0123456789":
            code = self.encode_char(d)
            out[d] = self.width_of_code(code) if code is not None else None
        return out

    @property
    def tabular_digits(self) -> Optional[bool]:
        """True if the ten digits all have exactly the same advance width.

        This is *the* property that makes a date replacement pixel-perfect:
        digit for digit, the line does not move one iota. It is the default
        case for the vast majority of body text fonts.
        """
        vals = [v for v in self.digit_widths.values() if v is not None]
        if len(vals) < 2:
            return None
        return max(vals) - min(vals) < 0.5

    # ------------------------------------------------------------------
    # Encoding
    # ------------------------------------------------------------------

    def encode_char(self, ch: str) -> Optional[int]:
        """Unicode -> PDF code. Prefers a code already used in the document."""
        codes = self.from_unicode.get(ch)
        if not codes:
            return None
        for c in codes:
            if c in self.used_codes:
                return c
        if self.program_codes:
            for c in codes:
                if c in self.program_codes:
                    return c
        return codes[0]

    def encode_text(self, text: str) -> Optional[bytes]:
        """Unicode -> bytes ready to be injected into the content stream."""
        out = bytearray()
        for ch in text:
            code = self.encode_char(ch)
            if code is None:
                return None
            if self.code_len == 2:
                if not 0 <= code <= 0xFFFF:
                    return None
                out += code.to_bytes(2, "big")
            else:
                if not 0 <= code <= 0xFF:
                    return None
                out.append(code)
        return bytes(out)

    def decode(self, data: bytes) -> List[Tuple[int, str]]:
        """Stream bytes -> [(code, unicode text)]."""
        out: List[Tuple[int, str]] = []
        if self.code_len == 2:
            if len(data) % 2:
                data = data[:-1]
            for i in range(0, len(data), 2):
                code = int.from_bytes(data[i : i + 2], "big")
                out.append((code, self.to_unicode.get(code, "")))
        else:
            for b in data:
                out.append((b, self.to_unicode.get(b, "")))
        return out

    # ------------------------------------------------------------------
    # Coverage
    # ------------------------------------------------------------------

    def coverage(self, ch: str) -> Tuple[str, Optional[int]]:
        """(confidence level, code) for a given character."""
        codes = self.from_unicode.get(ch)
        if not codes:
            return ABSENT, None
        for c in codes:
            if c in self.used_codes:
                return CERTAIN, c
        for c in codes:
            if c in self.repaired_codes:
                return PROGRAMME, c
        if self.program_codes is not None:
            for c in codes:
                if c in self.program_codes:
                    return PROGRAMME, c
            return ABSENT, codes[0]

        # Font program not inspectable. If the font is not embedded, the viewer
        # has a complete font at hand: no risk. If it is embedded, what it
        # declares proves nothing — a subset very often keeps a complete
        # encoding while drawing only a handful of glyphs.
        if not self.embedded:
            return DECLARE, codes[0]
        return INCERTAIN, codes[0]

    def coverage_of_text(self, text: str) -> Tuple[str, List[str]]:
        """Worst confidence level over a string + characters to secure."""
        worst = CERTAIN
        missing: List[str] = []
        for ch in set(text):
            lvl, _ = self.coverage(ch)
            if _CONFIDENCE_ORDER[lvl] > _CONFIDENCE_ORDER[worst]:
                worst = lvl
            if lvl in (ABSENT, INCERTAIN):
                missing.append(ch)
        return worst, sorted(missing)

    def summary(self) -> dict:
        return {
            "key": self.key,
            "name": self.display_name,
            "subtype": self.subtype,
            "embedded": self.embedded,
            "subset": self.subset_prefix is not None,
            "subset_prefix": self.subset_prefix,
            "code_len": self.code_len,
            "glyph_count": len(self.program_codes) if self.program_codes is not None else None,
            "used_glyphs": len(self.used_codes),
            "tabular_digits": self.tabular_digits,
            "digits_available": sorted(
                d for d in "0123456789"
                if self.coverage(d)[0] not in (ABSENT, INCERTAIN)
            ),
            "digits_missing": sorted(
                d for d in "0123456789"
                if self.coverage(d)[0] in (ABSENT, INCERTAIN)
            ),
            "inspectable": self.program_codes is not None,
            "program_error": self.program_error,
            "editable": self.editable,
            "reason": self.not_editable_reason,
        }


# --------------------------------------------------------------------------
# Building from the PDF font dictionary
# --------------------------------------------------------------------------


def _parse_w_array(w_arr) -> Dict[int, float]:
    """Parse the /W array of a CIDFont: [c [w...] | cfirst clast w]*"""
    widths: Dict[int, float] = {}
    items = list(w_arr)
    i = 0
    while i < len(items):
        try:
            first = int(items[i])
        except Exception:
            i += 1
            continue
        if i + 1 >= len(items):
            break
        nxt = items[i + 1]
        if isinstance(nxt, pikepdf.Array) or isinstance(nxt, list):
            for k, w in enumerate(list(nxt)):
                v = _num(w)
                if v is not None:
                    widths[first + k] = v
            i += 2
        else:
            if i + 2 >= len(items):
                break
            try:
                last = int(nxt)
                val = _num(items[i + 2], 0.0) or 0.0
            except Exception:
                i += 3
                continue
            if last >= first and last - first < 65536:
                for c in range(first, last + 1):
                    widths[c] = val
            i += 3
    return widths


def _extract_program_codes(
    font_bytes: bytes, kind: str, code_len: int, cid_to_gid_identity: bool,
    encoding_names: Optional[Dict[int, str]] = None,
    cid_to_gid: Optional[bytes] = None,
) -> Tuple[Optional[Set[int]], Optional[str]]:
    """
    Open the embedded font file and work out which *PDF codes* really have a
    drawn glyph behind them.
    """
    try:
        from fontTools.ttLib import TTFont
    except Exception as e:  # pragma: no cover
        return None, "fontTools unavailable: %s" % e

    try:
        if kind in ("FontFile2", "FontFile3-OpenType"):
            tt = TTFont(io.BytesIO(font_bytes), fontNumber=0, lazy=True,
                        ignoreDecompileErrors=True)
        elif kind == "FontFile3":
            # Bare CFF (Type1C / CIDFontType0C): fontTools can open it via CFFFontSet
            from fontTools.cffLib import CFFFontSet

            cff = CFFFontSet()
            cff.decompile(io.BytesIO(font_bytes), None)
            td = cff[cff.fontNames[0]]
            charset = list(getattr(td, "charset", []) or [])
            if getattr(td, "ROS", None) is not None:
                # CFF CIDFont: the charset holds CIDs (cidXXXX)
                cids: Set[int] = set()
                for i, nm in enumerate(charset):
                    if isinstance(nm, str) and nm.startswith("cid"):
                        try:
                            cids.add(int(nm[3:]))
                        except ValueError:
                            cids.add(i)
                    else:
                        cids.add(i)
                return cids, None
            names = set(charset)
            if encoding_names:
                return {c for c, n in encoding_names.items() if n in names}, None
            # with no explicit encoding: fall back on the unicode of the names
            out: Set[int] = set()
            for i, nm in enumerate(charset):
                out.add(i)
            return out, None
        else:
            return None, "font format not analysable (%s)" % kind
    except Exception as e:
        return None, "embedded font unreadable: %s" % type(e).__name__

    try:
        num_glyphs = tt.get("maxp").numGlyphs if "maxp" in tt else None
    except Exception:
        num_glyphs = None

    # Which GIDs have a non-empty outline?
    drawn: Optional[Set[int]] = None
    try:
        if "glyf" in tt:
            glyf = tt["glyf"]
            order = tt.getGlyphOrder()
            drawn = set()
            for gid, name in enumerate(order):
                try:
                    g = glyf[name]
                    if g.numberOfContours != 0:
                        drawn.add(gid)
                except Exception:
                    drawn.add(gid)
        elif "CFF " in tt:
            cff = tt["CFF "].cff
            td = cff[cff.fontNames[0]]
            cs = td.CharStrings
            order = tt.getGlyphOrder()
            drawn = set()
            for gid, name in enumerate(order):
                try:
                    if name in cs and len(cs[name].bytecode or b"") > 2:
                        drawn.add(gid)
                except Exception:
                    drawn.add(gid)
    except Exception:
        drawn = None

    def gid_ok(gid: int) -> bool:
        if num_glyphs is not None and gid >= num_glyphs:
            return False
        if gid == 0:
            return False  # .notdef
        if drawn is not None:
            return gid in drawn
        return True

    # --- composite font (CID) -----------------------------------------------
    if code_len == 2:
        if cid_to_gid is not None:
            # Explicit CID -> GID table: two bytes per CID, big-endian. Very
            # common (Chrome, wkhtmltopdf, ReportLab) and full of zeros: only
            # a few dozen CIDs really point at a drawing.
            n = len(cid_to_gid) // 2
            out: Set[int] = set()
            for cid in range(n):
                gid = (cid_to_gid[2 * cid] << 8) | cid_to_gid[2 * cid + 1]
                if gid and gid_ok(gid):
                    out.add(cid)
            return out, None
        if cid_to_gid_identity:
            limit = num_glyphs if num_glyphs else 65536
            return {c for c in range(1, min(limit, 65536)) if gid_ok(c)}, None
        return None, "CIDToGIDMap unreadable"

    # --- simple font: code -> gid via cmap or glyph order -------------------
    codes: Set[int] = set()
    cmap_tables = []
    try:
        if "cmap" in tt:
            cmap_tables = tt["cmap"].tables
    except Exception:
        cmap_tables = []

    sym = {t for t in cmap_tables if t.platformID == 3 and t.platEncID == 0}
    uni = [t for t in cmap_tables if (t.platformID, t.platEncID) in
           ((3, 1), (3, 10), (0, 3), (0, 4), (0, 6))]
    mac = [t for t in cmap_tables if (t.platformID, t.platEncID) == (1, 0)]

    name_to_gid = {}
    try:
        name_to_gid = {n: i for i, n in enumerate(tt.getGlyphOrder())}
    except Exception:
        pass

    for code in range(256):
        gid = None
        # 1. symbolic (3,0) cmap: raw code or 0xF000+code
        for t in sym:
            m = t.cmap
            for probe in (0xF000 + code, code):
                if probe in m:
                    gid = name_to_gid.get(m[probe])
                    break
            if gid is not None:
                break
        # 2. through the glyph name from the PDF encoding
        if gid is None and encoding_names:
            nm = encoding_names.get(code)
            if nm:
                if nm in name_to_gid:
                    gid = name_to_gid[nm]
                else:
                    u = glyph_name_to_unicode(nm)
                    if u and len(u) == 1:
                        for t in uni:
                            if ord(u) in t.cmap:
                                gid = name_to_gid.get(t.cmap[ord(u)])
                                break
        # 3. Mac (1,0) cmap on the raw code
        if gid is None:
            for t in mac:
                if code in t.cmap:
                    gid = name_to_gid.get(t.cmap[code])
                    break
        if gid is not None and gid_ok(gid):
            codes.add(code)

    return (codes if codes else None), (None if codes else "no code resolved via cmap")


_BASE14_ALIASES = {
    "arial": "helv", "arialmt": "helv", "arialbold": "hebo",
    "arialboldmt": "hebo", "arialitalic": "heit", "helvetica": "helv",
    "helveticabold": "hebo", "helveticaoblique": "heit",
    "helveticaboldoblique": "hebi", "timesnewroman": "tiro",
    "timesnewromanpsmt": "tiro", "timesroman": "tiro", "times": "tiro",
    "timesbold": "tibo", "timesitalic": "tiit", "timesbolditalic": "tibi",
    "couriernew": "cour", "couriernewpsmt": "cour", "courier": "cour",
    "courierbold": "cobo", "symbol": "symb", "zapfdingbats": "zadb",
}


def _base14_alias(name: str) -> Optional[str]:
    n = re.sub(r"[^A-Za-z]", "", (name or "").split("+")[-1]).lower()
    if n in _BASE14_ALIASES:
        return _BASE14_ALIASES[n]
    for key, alias in _BASE14_ALIASES.items():
        if n.startswith(key):
            return alias
    return None


def _fill_missing_widths(fi: FontInfo, font_bytes, file_kind, enc_names) -> None:
    """
    /Widths is supposed to be mandatory, but the 14 standard fonts are exempt
    from it and some producers omit it or fill it with zeros. Without correct
    advance widths there is no way to compensate the layout, so we go look for
    them elsewhere.
    """
    have = [w for w in fi.widths.values() if w > 0]
    if len(have) >= max(4, len(fi.widths) // 2):
        return

    # 1. hmtx of the embedded font
    if font_bytes and file_kind in ("FontFile2", "FontFile3-OpenType"):
        try:
            from fontTools.ttLib import TTFont

            tt = TTFont(io.BytesIO(font_bytes), fontNumber=0, lazy=True,
                        ignoreDecompileErrors=True)
            upem = float(tt["head"].unitsPerEm) if "head" in tt else 1000.0
            hmtx = tt["hmtx"] if "hmtx" in tt else None
            order = tt.getGlyphOrder()
            if hmtx is not None and upem > 0:
                scale = 1000.0 / upem
                if fi.code_len == 2:
                    for gid, gname in enumerate(order):
                        try:
                            fi.widths.setdefault(gid, hmtx[gname][0] * scale)
                        except Exception:
                            pass
                elif enc_names:
                    for code, gname in enc_names.items():
                        try:
                            if gname in hmtx.metrics:
                                fi.widths.setdefault(code, hmtx[gname][0] * scale)
                        except Exception:
                            pass
                fi.widths = {k: v for k, v in fi.widths.items() if v > 0} or fi.widths
                if [w for w in fi.widths.values() if w > 0]:
                    return
        except Exception:
            pass

    # 2. built-in metrics of the 14 standard fonts (via MuPDF)
    alias = _base14_alias(fi.base_font or fi.display_name)
    if alias:
        try:
            import fitz

            mf = fitz.Font(fontname=alias)
            filled = {}
            for code, u in fi.to_unicode.items():
                if len(u) == 1:
                    adv = mf.glyph_advance(ord(u))
                    if adv:
                        filled[code] = round(adv * 1000.0, 2)
            if filled:
                fi.widths.update(filled)
                if fi.default_width == 0.0:
                    fi.default_width = round(mf.glyph_advance(ord(" ")) * 1000.0, 2)
        except Exception:
            pass


def analyze_font(key: str, font_obj, cache: Dict[str, FontInfo]) -> FontInfo:
    """Analyze a PDF font dictionary (cached by object identity)."""
    try:
        objgen = "%d_%d" % font_obj.objgen if hasattr(font_obj, "objgen") else str(id(font_obj))
    except Exception:
        objgen = str(id(font_obj))

    cached = cache.get(objgen)
    if cached is not None:
        return cached

    fi = FontInfo(key=key, objgen=objgen)
    cache[objgen] = fi

    fi.subtype = _s(font_obj.get("/Subtype")) or ""
    fi.base_font = _s(font_obj.get("/BaseFont")) or ""
    m = _SUBSET_PREFIX_RE.match(fi.base_font)
    if m:
        fi.subset_prefix, fi.display_name = m.group(1), m.group(2)
    else:
        fi.display_name = fi.base_font or "(unnamed)"

    if fi.subtype == "/Type3" or fi.subtype == "Type3":
        fi.editable = False
        fi.not_editable_reason = "Type 3 font (glyphs = drawing programs)"

    # --- ToUnicode ---------------------------------------------------------
    cm: Optional[CMap] = None
    tu = font_obj.get("/ToUnicode")
    if tu is not None:
        try:
            cm = parse_tounicode(bytes(tu.read_bytes()))
            fi.to_unicode.update(cm.to_unicode)
        except Exception:
            cm = None

    is_type0 = fi.subtype == "Type0"

    # --- descendant / descriptor -------------------------------------------
    desc_font = None
    if is_type0:
        df = font_obj.get("/DescendantFonts")
        try:
            desc_font = df[0] if df is not None and len(df) else None
        except Exception:
            desc_font = None
        enc = font_obj.get("/Encoding")
        enc_name = _s(enc) if isinstance(enc, pikepdf.Name) else None
        fi.encoding_name = enc_name
        if enc_name in ("Identity-H", "Identity-V"):
            fi.code_len = 2
        elif enc is not None and not isinstance(enc, pikepdf.Name):
            fi.code_len = 2  # embedded CMap: we assume 2 bytes
            fi.cmap_supported = False
            fi.editable = False
            fi.not_editable_reason = "embedded encoding CMap (not Identity)"
        else:
            fi.code_len = 2
            fi.cmap_supported = False
            fi.editable = False
            fi.not_editable_reason = "predefined CMap %s not supported" % (enc_name or "?")
    else:
        fi.code_len = 1

    descriptor = None
    if desc_font is not None:
        descriptor = desc_font.get("/FontDescriptor")
    if descriptor is None:
        descriptor = font_obj.get("/FontDescriptor")

    builtin_names: Optional[Dict[int, str]] = None
    font_bytes: Optional[bytes] = None
    file_kind: Optional[str] = None

    if descriptor is not None:
        flags = int(_num(descriptor.get("/Flags"), 0) or 0)
        fi.symbolic = bool(flags & 4) and not (flags & 32)
        fi.default_width = _num(descriptor.get("/MissingWidth"), 0.0) or 0.0
        asc = _num(descriptor.get("/Ascent"))
        desc = _num(descriptor.get("/Descent"))
        if asc and 200 <= abs(asc) <= 1400:
            fi.ascent = abs(asc)
        if desc and 20 <= abs(desc) <= 800:
            fi.descent = -abs(desc)
        for k in ("/FontFile2", "/FontFile3", "/FontFile"):
            ff = descriptor.get(k)
            if ff is not None:
                fi.embedded = True
                fi.font_file_key = k.lstrip("/")
                try:
                    font_bytes = bytes(ff.read_bytes())
                except Exception:
                    font_bytes = None
                st = _s(ff.get("/Subtype")) if hasattr(ff, "get") else None
                if k == "/FontFile2":
                    file_kind = "FontFile2"
                elif k == "/FontFile3":
                    file_kind = "FontFile3-OpenType" if st == "OpenType" else "FontFile3"
                else:
                    file_kind = "FontFile"
                break

    if not fi.embedded and fi.display_name.split(",")[0].split("-")[0] in {
        n.split(",")[0].split("-")[0] for n in STANDARD_14
    }:
        fi.embedded = False

    # --- encoding of simple fonts ------------------------------------------
    enc_names: Optional[Dict[int, str]] = None
    if not is_type0:
        enc = font_obj.get("/Encoding")
        base_enc_name = None
        differences = None
        if isinstance(enc, pikepdf.Name):
            base_enc_name = _s(enc)
        elif enc is not None:
            base_enc_name = _s(enc.get("/BaseEncoding")) if enc.get("/BaseEncoding") else None
            d = enc.get("/Differences")
            if d is not None:
                differences = []
                for it in d:
                    if isinstance(it, pikepdf.Name):
                        differences.append(_s(it))
                    else:
                        v = _num(it)
                        if v is not None:
                            differences.append(int(v))
        fi.encoding_name = base_enc_name

        # internal encoding of the embedded font (symbolic fonts)
        if font_bytes and file_kind in ("FontFile2", "FontFile3-OpenType"):
            try:
                from fontTools.ttLib import TTFont

                tt = TTFont(io.BytesIO(font_bytes), fontNumber=0, lazy=True,
                            ignoreDecompileErrors=True)
                if "cmap" in tt:
                    for t in tt["cmap"].tables:
                        if (t.platformID, t.platEncID) == (1, 0):
                            builtin_names = {c: n for c, n in t.cmap.items() if c < 256}
                            break
            except Exception:
                builtin_names = None

        enc_names, enc_uni = build_simple_encoding(
            base_enc_name, differences, symbolic=fi.symbolic, builtin=builtin_names
        )
        # ToUnicode wins over the deduced encoding (it is what the producer states)
        for code, u in enc_uni.items():
            fi.to_unicode.setdefault(code, u)

    # --- advance widths ----------------------------------------------------
    if is_type0 and desc_font is not None:
        fi.default_width = _num(desc_font.get("/DW"), 1000.0) or 1000.0
        w = desc_font.get("/W")
        if w is not None:
            try:
                fi.widths = _parse_w_array(w)
            except Exception:
                fi.widths = {}
        c2g = desc_font.get("/CIDToGIDMap")
        cid_identity = c2g is None or _s(c2g) == "Identity"
        if not cid_identity and hasattr(c2g, "read_bytes"):
            try:
                fi.cid_to_gid = bytes(c2g.read_bytes())
            except Exception:
                fi.cid_to_gid = None
    else:
        cid_identity = True
        first = int(_num(font_obj.get("/FirstChar"), 0) or 0)
        warr = font_obj.get("/Widths")
        if warr is not None:
            try:
                for i, wv in enumerate(list(warr)):
                    v = _num(wv)
                    if v is not None:
                        fi.widths[first + i] = v
            except Exception:
                pass

    # --- fall back on metrics when /Widths is missing or zero --------------
    _fill_missing_widths(fi, font_bytes, file_kind, enc_names)

    # --- real coverage from the font file ----------------------------------
    if font_bytes and file_kind:
        fi.program_codes, fi.program_error = _extract_program_codes(
            font_bytes, file_kind, fi.code_len, cid_identity, enc_names,
            fi.cid_to_gid
        )
    elif not fi.embedded:
        fi.program_error = "font not embedded (system metrics)"

    # --- reverse map -------------------------------------------------------
    rev: Dict[str, List[int]] = {}
    for code, s in fi.to_unicode.items():
        if s:
            rev.setdefault(s, []).append(code)
    for s in rev:
        rev[s].sort()
    fi.from_unicode = rev

    if fi.editable and not fi.from_unicode:
        fi.editable = False
        fi.not_editable_reason = "no usable code->Unicode mapping"

    return fi
