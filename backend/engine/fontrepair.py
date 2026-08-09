"""
Font repair: inject into an embedded subset the glyphs it is missing, taken
from the original font.

This is what takes the tool from "it often works" to "it works".

The typical scenario: an invoice writes "15/03/2024". The generator embedded a
subset of the font containing only the glyphs used on the page — so 0,1,2,3,4,5
but neither 6 nor 8. Replacing it with "28/11/2025" needs an 8 that does not
exist in the file. Two ways out: show a blank box (what the tools on the market
do), or go fetch the real 8 in the source font and add it cleanly to the
subset. We do the second.

Method, for a CIDFontType2 in Identity-H (the dominant case today):
  1. allocate *fresh* CIDs at the end of the glyph table — no risk of collision
     with the existing CIDs, no renumbering;
  2. draw the source glyph into the subset, rescaling it if the units per em
     differ;
  3. update /W (advance widths), /ToUnicode (reading) and the font program.

The result is still *the same font*, simply less stripped-down.
"""

from __future__ import annotations

import io
import os
import re
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Dict, List, Optional, Sequence, Set, Tuple

import pikepdf

from .fonts import FontInfo
from .glyphlist import unicode_to_glyph_name

FONT_DIRS = [
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "/srv/pdf/data/fonts",
]

_NAME_CLEAN = re.compile(r"[^a-z0-9]")


def _norm(s: str) -> str:
    return _NAME_CLEAN.sub("", (s or "").lower())


# --------------------------------------------------------------------------
# Index of the available fonts
# --------------------------------------------------------------------------


@dataclass
class FontFileEntry:
    path: str
    index: int = 0
    ps_name: str = ""
    family: str = ""
    full_name: str = ""
    subfamily: str = ""

    # Not all keys are equal. The PostScript name identifies one precise
    # weight; the family name is shared by every weight. With no hierarchy,
    # "DejaVuSans" landed on DejaVuSans-Bold simply because that file is
    # walked first — and the grafted glyphs came out bold in the middle of
    # light text.
    @property
    def ranked_keys(self) -> List[Tuple[int, str]]:
        out = [(3, _norm(self.ps_name)),
               (2, _norm(self.full_name)),
               (1, _norm(self.family + self.subfamily)),
               (0, _norm(self.family))]
        return [(r, k) for r, k in out if k]

    @property
    def keys(self) -> Set[str]:
        return {k for _r, k in self.ranked_keys}

    @property
    def is_regular(self) -> bool:
        sub = (self.subfamily or "").strip().lower()
        return sub in ("", "regular", "book", "roman", "normal", "medium")


class FontLibrary:
    """Index of the font files usable as a repair source."""

    def __init__(self, extra_dirs: Optional[Sequence[str]] = None):
        self.entries: List[FontFileEntry] = []
        self.by_key: Dict[str, FontFileEntry] = {}
        self._key_rank: Dict[str, int] = {}
        self._dirs = list(FONT_DIRS) + list(extra_dirs or [])
        self._scanned = False

    def scan(self) -> None:
        if self._scanned:
            return
        self._scanned = True
        from fontTools.ttLib import TTCollection, TTFont

        seen = set()
        for root_dir in self._dirs:
            if not os.path.isdir(root_dir):
                continue
            for root, _dirs, files in os.walk(root_dir):
                for fn in files:
                    ext = os.path.splitext(fn)[1].lower()
                    if ext not in (".ttf", ".otf", ".ttc", ".otc"):
                        continue
                    path = os.path.join(root, fn)
                    if path in seen:
                        continue
                    seen.add(path)
                    try:
                        if ext in (".ttc", ".otc"):
                            coll = TTCollection(path, lazy=True)
                            fonts = [(f, i) for i, f in enumerate(coll.fonts)]
                        else:
                            fonts = [(TTFont(path, lazy=True,
                                             ignoreDecompileErrors=True), 0)]
                    except Exception:
                        continue
                    for tt, idx in fonts:
                        try:
                            e = self._entry_from(tt, path, idx)
                        except Exception:
                            continue
                        finally:
                            try:
                                tt.close()
                            except Exception:
                                pass
                        if e is None:
                            continue
                        self.entries.append(e)
                        self._index(e)

    def _index(self, e: FontFileEntry) -> None:
        """A stronger key wins; at equal strength, the regular weight wins."""
        for rank, k in e.ranked_keys:
            cur_rank = self._key_rank.get(k)
            if cur_rank is None or rank > cur_rank:
                self.by_key[k] = e
                self._key_rank[k] = rank
            elif rank == cur_rank and e.is_regular and not self.by_key[k].is_regular:
                self.by_key[k] = e

    @staticmethod
    def _entry_from(tt, path: str, idx: int) -> Optional[FontFileEntry]:
        name = tt.get("name")
        if name is None:
            return None

        def g(nid: int) -> str:
            for plat in ((3, 1, 0x409), (1, 0, 0)):
                r = name.getName(nid, *plat)
                if r is not None:
                    try:
                        return str(r)
                    except Exception:
                        pass
            r = name.getDebugName(nid)
            return r or ""

        return FontFileEntry(path=path, index=idx, ps_name=g(6), family=g(1),
                             full_name=g(4), subfamily=g(2))

    # -- lookup -----------------------------------------------------------

    def find(self, base_font: str) -> Optional[FontFileEntry]:
        """Find the font file matching a PDF /BaseFont."""
        self.scan()
        raw = (base_font or "").split("+")[-1]
        candidates = [raw, raw.replace(",", "-"), raw.split(",")[0],
                      raw.split("-")[0]]

        for c in candidates:
            e = self.by_key.get(_norm(c))
            if e is not None:
                return e

        # Prefix match, for a name the generator truncated. We want the
        # *closest* neighbour, so the shortest key that still covers the
        # target — definitely not the longest one, which would add a weight or
        # a variant nobody asked for.
        target = _norm(raw)
        if len(target) >= 5:
            best: Optional[FontFileEntry] = None
            best_len = None
            for k, e in self.by_key.items():
                if len(k) < 5 or not (k.startswith(target) or target.startswith(k)):
                    continue
                score = abs(len(k) - len(target))
                if best_len is None or score < best_len or \
                        (score == best_len and e.is_regular and not best.is_regular):
                    best, best_len = e, score
            if best is not None:
                return best

        return self._fontconfig(raw)

    @staticmethod
    def _fontconfig(name: str) -> Optional[FontFileEntry]:
        """Last resort: ask fontconfig for the nearest neighbour.

        Careful, fc-match *always* answers something. So it is no longer the
        original font but an approximation — the caller has to say so.
        """
        try:
            out = subprocess.run(
                ["fc-match", "-f", "%{file}\t%{postscriptname}\t%{family}",
                 name.replace("-", " ")],
                capture_output=True, timeout=5, text=True)
            if out.returncode != 0 or not out.stdout.strip():
                return None
            parts = out.stdout.split("\t")
            if not parts[0] or not os.path.exists(parts[0]):
                return None
            return FontFileEntry(path=parts[0], ps_name=parts[1] if len(parts) > 1 else "",
                                 family=parts[2] if len(parts) > 2 else "")
        except Exception:
            return None


@lru_cache(maxsize=1)
def default_library() -> FontLibrary:
    lib = FontLibrary()
    lib.scan()
    return lib


# --------------------------------------------------------------------------
# Result
# --------------------------------------------------------------------------


@dataclass
class RepairResult:
    ok: bool
    added: List[str] = field(default_factory=list)
    source_path: Optional[str] = None
    source_name: Optional[str] = None
    exact_source: bool = False       # True = the real original font was found
    reason: Optional[str] = None
    new_codes: Dict[str, int] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Repair
# --------------------------------------------------------------------------


def _pdf_font_objects(font_obj) -> Tuple[object, object, object]:
    """(font dict, descendant dict or None, descriptor)"""
    desc_font = None
    subtype = str(font_obj.get("/Subtype") or "")
    if subtype == "/Type0":
        df = font_obj.get("/DescendantFonts")
        try:
            desc_font = df[0] if df is not None and len(df) else None
        except Exception:
            desc_font = None
    holder = desc_font if desc_font is not None else font_obj
    return font_obj, desc_font, holder.get("/FontDescriptor")


def _build_tounicode(mapping: Dict[int, str]) -> bytes:
    """Build a complete /ToUnicode stream from a code->text table."""
    items = sorted(mapping.items())
    chunks: List[str] = []
    for i in range(0, len(items), 100):
        block = items[i : i + 100]
        lines = []
        for code, s in block:
            dst = "".join("%04X" % u for u in
                          [ord(c) for c in s] if u <= 0xFFFF) or "FFFD"
            # characters outside the BMP must be written as surrogate pairs
            if any(ord(c) > 0xFFFF for c in s):
                dst = ""
                for c in s:
                    cp = ord(c)
                    if cp > 0xFFFF:
                        cp -= 0x10000
                        dst += "%04X%04X" % (0xD800 + (cp >> 10), 0xDC00 + (cp & 0x3FF))
                    else:
                        dst += "%04X" % cp
            lines.append("<%04X> <%s>" % (code, dst))
        chunks.append("%d beginbfchar\n%s\nendbfchar" % (len(block), "\n".join(lines)))

    body = "\n".join(chunks)
    return ("""/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def
/CMapName /Adobe-Identity-UCS def
/CMapType 2 def
1 begincodespacerange
<0000> <FFFF>
endcodespacerange
%s
endcmap
CMapName currentdict /CMap defineresource pop
end
end""" % body).encode("ascii", "replace")


def _w_array_from(widths: Dict[int, float], default: float) -> pikepdf.Array:
    """Rewrite /W compactly as [c [w...]], grouping contiguous CIDs."""
    out: List = []
    codes = sorted(c for c, w in widths.items() if abs(w - default) > 0.01)
    i = 0
    while i < len(codes):
        j = i
        while j + 1 < len(codes) and codes[j + 1] == codes[j] + 1:
            j += 1
        out.append(codes[i])
        out.append(pikepdf.Array([round(widths[c], 2) for c in codes[i : j + 1]]))
        i = j + 1
    return pikepdf.Array(out)


def repair_font(pdf: pikepdf.Pdf, font_obj, fi: FontInfo, needed: Sequence[str],
                library: Optional[FontLibrary] = None,
                source_path: Optional[str] = None) -> RepairResult:
    """
    Add to the embedded subset the `needed` characters it is missing.

    Modifies the PDF in place and updates `fi` so the surgery that follows
    sees the new glyphs immediately.
    """
    missing = [ch for ch in dict.fromkeys(needed)
               if fi.coverage(ch)[0] in ("absent", "unverified")]
    if not missing:
        return RepairResult(ok=True, added=[], reason="nothing to repair")

    if not fi.embedded:
        return RepairResult(ok=False, reason="font not embedded: nothing to repair, "
                                             "the viewer will use its own font")
    if fi.font_file_key != "FontFile2":
        return RepairResult(
            ok=False,
            reason="%s font program (PostScript/CFF): glyph injection is not "
                   "supported" % (fi.font_file_key or "?"))

    # --- source font -------------------------------------------------------
    entry: Optional[FontFileEntry] = None
    exact = False
    if source_path and os.path.exists(source_path):
        entry = FontFileEntry(path=source_path, ps_name=os.path.basename(source_path))
        exact = True
    else:
        lib = library or default_library()
        entry = lib.find(fi.base_font or fi.display_name)
        if entry is not None:
            exact = _norm(entry.ps_name) == _norm((fi.base_font or "").split("+")[-1]) \
                or _norm(entry.full_name) == _norm(fi.display_name)
    if entry is None:
        return RepairResult(ok=False, reason="no source font found for \"%s\""
                                             % fi.display_name)

    try:
        from fontTools.pens.recordingPen import DecomposingRecordingPen
        from fontTools.pens.transformPen import TransformPen
        from fontTools.pens.ttGlyphPen import TTGlyphPen
        from fontTools.ttLib import TTFont
    except Exception as e:
        return RepairResult(ok=False, reason="fontTools unavailable: %s" % e)

    font_dict, desc_font, descriptor = _pdf_font_objects(font_obj)
    if descriptor is None:
        return RepairResult(ok=False, reason="font descriptor missing")
    ff = descriptor.get("/FontFile2")
    if ff is None:
        return RepairResult(ok=False, reason="FontFile2 missing")

    try:
        sub = TTFont(io.BytesIO(bytes(ff.read_bytes())), fontNumber=0,
                     ignoreDecompileErrors=True)
    except Exception as e:
        return RepairResult(ok=False, reason="subset unreadable: %s" % e)

    if "glyf" not in sub:
        return RepairResult(ok=False, reason="embedded font with no glyf table "
                                             "(PostScript outlines)")

    try:
        src = TTFont(entry.path, fontNumber=entry.index, ignoreDecompileErrors=True)
    except Exception as e:
        return RepairResult(ok=False, reason="source font unreadable: %s" % e)

    try:
        return _inject_glyphs(pdf, sub, src, fi, missing, entry, exact,
                              font_dict, desc_font, descriptor, ff,
                              DecomposingRecordingPen, TransformPen, TTGlyphPen)
    finally:
        try:
            src.close()
        except Exception:
            pass


def _inject_glyphs(pdf, sub, src, fi: FontInfo, missing: List[str],
                   entry: FontFileEntry, exact: bool,
                   font_dict, desc_font, descriptor, ff,
                   DecomposingRecordingPen, TransformPen, TTGlyphPen) -> RepairResult:
    src_upem = float(src["head"].unitsPerEm) if "head" in src else 1000.0
    sub_upem = float(sub["head"].unitsPerEm) if "head" in sub else 1000.0
    scale = (sub_upem / src_upem) if src_upem else 1.0

    src_cmap = {}
    try:
        src_cmap = src.getBestCmap() or {}
    except Exception:
        pass
    if not src_cmap:
        return RepairResult(ok=False, reason="source font with no Unicode cmap table")

    src_glyphs = src.getGlyphSet()
    src_hmtx = src["hmtx"] if "hmtx" in src else None

    glyf = sub["glyf"]
    hmtx = sub["hmtx"]
    order = list(sub.getGlyphOrder())
    base_count = len(order)

    added: List[str] = []
    new_gids: Dict[str, int] = {}      # character -> index of the added glyph
    new_codes: Dict[str, int] = {}     # character -> code written in the stream
    skipped: List[str] = []

    for ch in missing:
        cp = ord(ch)
        src_name = src_cmap.get(cp)
        if src_name is None or src_name not in src_glyphs:
            skipped.append(ch)
            continue

        try:
            rec = DecomposingRecordingPen(src_glyphs)
            src_glyphs[src_name].draw(rec)
            pen = TTGlyphPen(None)
            if abs(scale - 1.0) > 1e-9:
                rec.replay(TransformPen(pen, (scale, 0, 0, scale, 0, 0)))
            else:
                rec.replay(pen)
            new_glyph = pen.glyph()
        except Exception:
            skipped.append(ch)
            continue

        # Give the glyph its canonical AGL name ("six", "eacute"...): a simple
        # font resolves /Differences by name, so a standard name maximizes the
        # chances that any viewer finds its way.
        base_name = unicode_to_glyph_name(ch) or ("uni%04X" % cp)
        gname = base_name
        n = 0
        while gname in glyf.glyphs or gname in order:
            n += 1
            gname = "%s.r%d" % (base_name, n)

        try:
            adv, lsb = (src_hmtx[src_name] if src_hmtx and src_name in src_hmtx.metrics
                        else (int(src_upem * 0.5), 0))
        except Exception:
            adv, lsb = int(src_upem * 0.5), 0

        glyf.glyphs[gname] = new_glyph
        order.append(gname)
        hmtx.metrics[gname] = (int(round(adv * scale)), int(round(lsb * scale)))
        added.append(ch)
        new_gids[ch] = base_count + len(added) - 1

    if not added:
        return RepairResult(
            ok=False, source_path=entry.path, source_name=entry.ps_name or entry.family,
            exact_source=exact,
            reason="the source font provides none of the requested glyphs (%s)"
                   % " ".join(skipped))

    # --- recomposing the font program --------------------------------------
    try:
        sub.setGlyphOrder(order)
        glyf.glyphOrder = order
        if "maxp" in sub:
            sub["maxp"].numGlyphs = len(order)
        if fi.code_len == 2:
            # CID + Identity-H: the viewer goes from code to GID without going
            # through the cmap or the names. So we can trim.
            if "post" in sub:
                try:
                    sub["post"].formatType = 3.0
                    sub["post"].glyphOrder = order
                except Exception:
                    pass
            if "cmap" in sub:
                try:
                    del sub["cmap"]
                except Exception:
                    pass
        else:
            # Simple font: /Differences designates the glyphs *by name*. So
            # the post table has to stay in format 2.0 and know the new names;
            # we back it up with a Unicode cmap entry.
            if "post" in sub:
                try:
                    sub["post"].formatType = 2.0
                    sub["post"].glyphOrder = order
                    if hasattr(sub["post"], "extraNames"):
                        sub["post"].extraNames = []
                    if hasattr(sub["post"], "mapping"):
                        sub["post"].mapping = {}
                except Exception:
                    pass
            _add_cmap_entries(sub, order, base_count)
        buf = io.BytesIO()
        sub.save(buf)
        new_bytes = buf.getvalue()
    except Exception as e:
        return RepairResult(ok=False, source_path=entry.path, exact_source=exact,
                            reason="cannot recompose the font: %s: %s"
                                   % (type(e).__name__, e))

    # --- writing into the PDF ----------------------------------------------
    ff.write(new_bytes)
    ff["/Length1"] = len(new_bytes)

    # --- assigning the codes -----------------------------------------------
    # For a composite font the code written in the stream is a CID, and going
    # from CID to glyph happens either by identity or through an explicit
    # table. Writing a fresh CID without updating that table leads straight to
    # glyph 0: the blank box. That is exactly what used to happen.
    c2g_obj = None
    c2g_data = None
    if fi.code_len == 2 and desc_font is not None:
        c2g_obj = desc_font.get("/CIDToGIDMap")
        if c2g_obj is not None and hasattr(c2g_obj, "read_bytes"):
            try:
                c2g_data = bytearray(c2g_obj.read_bytes())
            except Exception:
                c2g_data = None

    def _map_gid(cid: int) -> int:
        if c2g_data is None or 2 * cid + 1 >= len(c2g_data):
            return 0
        return (c2g_data[2 * cid] << 8) | c2g_data[2 * cid + 1]

    taken = set(fi.to_unicode.keys()) | set(fi.widths.keys())
    for ch in added:
        gid = new_gids[ch]
        if c2g_data is None:
            # Identity: the CID *is* the GID, nothing more to do.
            new_codes[ch] = gid
            continue
        # We happily reuse the CID the font already associates with this
        # character — producers often use the code point — provided it still
        # points at nothing.
        cid = None
        for cand in fi.from_unicode.get(ch, []):
            if _map_gid(cand) == 0:
                cid = cand
                break
        if cid is None:
            cand = ord(ch)
            if 0 < cand < 65536 and cand not in taken and _map_gid(cand) == 0:
                cid = cand
        if cid is None:
            cand = 1
            while cand < 65536 and (cand in taken or _map_gid(cand) != 0):
                cand += 1
            cid = cand
        taken.add(cid)
        needed = 2 * (cid + 1)
        if len(c2g_data) < needed:
            c2g_data.extend(b"\x00" * (needed - len(c2g_data)))
        c2g_data[2 * cid] = (gid >> 8) & 0xFF
        c2g_data[2 * cid + 1] = gid & 0xFF
        new_codes[ch] = cid

    if c2g_data is not None and c2g_obj is not None:
        try:
            c2g_obj.write(bytes(c2g_data))
        except Exception:
            return RepairResult(ok=False, source_path=entry.path, exact_source=exact,
                                reason="CID->GID table not rewritable")

    widths_added = {}
    for ch in added:
        gname = order[new_gids[ch]]
        widths_added[new_codes[ch]] = hmtx.metrics[gname][0] * 1000.0 / sub_upem

    if fi.code_len == 2 and desc_font is not None:
        fi.widths.update(widths_added)
        desc_font["/W"] = _w_array_from(fi.widths, fi.default_width)
        if "/CIDSet" in descriptor:
            del descriptor["/CIDSet"]
    else:
        # simple font: we also have to place the new glyphs in the encoding,
        # otherwise the code designates nothing.
        ok, err = _extend_simple_encoding(pdf, font_dict, fi, added, new_gids,
                                          new_codes, order, hmtx, sub_upem)
        if not ok:
            return RepairResult(ok=False, source_path=entry.path, exact_source=exact,
                                reason=err)

    # --- /ToUnicode --------------------------------------------------------
    for ch, code in new_codes.items():
        fi.to_unicode[code] = ch
        fi.from_unicode.setdefault(ch, []).append(code)
        fi.from_unicode[ch].sort()
        fi.repaired_codes.add(code)
        if fi.program_codes is not None:
            fi.program_codes.add(code)

    try:
        tu = pdf.make_stream(_build_tounicode(fi.to_unicode))
        font_dict["/ToUnicode"] = tu
    except Exception:
        pass

    return RepairResult(
        ok=True, added=added, source_path=entry.path,
        source_name=entry.ps_name or entry.full_name or entry.family,
        exact_source=exact, new_codes=new_codes,
        reason=None if not skipped else "not provided by the source: %s" % " ".join(skipped),
    )


def _add_cmap_entries(sub, order: List[str], base_count: int) -> None:
    """Add the repaired glyphs to the existing Unicode cmap subtables."""
    try:
        if "cmap" not in sub:
            return
        from .glyphlist import glyph_name_to_unicode

        for table in sub["cmap"].tables:
            if (table.platformID, table.platEncID) not in ((3, 1), (3, 10), (0, 3), (0, 4)):
                continue
            for gname in order[base_count:]:
                u = glyph_name_to_unicode(gname.split(".")[0])
                if u and len(u) == 1 and ord(u) not in table.cmap:
                    table.cmap[ord(u)] = gname
    except Exception:
        pass


def _extend_simple_encoding(pdf, font_dict, fi: FontInfo, added: List[str],
                            new_gids: Dict[str, int], new_codes: Dict[str, int],
                            order, hmtx, sub_upem: float) -> Tuple[bool, Optional[str]]:
    """
    For a simple font, a code is worth nothing without the encoding. We reserve
    free codes in /Differences and extend /Widths accordingly.
    """
    used = set(fi.to_unicode.keys()) | set(fi.widths.keys())
    free = [c for c in range(32, 256) if c not in used]
    if len(free) < len(added):
        return False, "no free code left in the font encoding"

    enc = font_dict.get("/Encoding")
    if enc is None or isinstance(enc, pikepdf.Name):
        base = enc if isinstance(enc, pikepdf.Name) else pikepdf.Name("/WinAnsiEncoding")
        enc = pikepdf.Dictionary(Type=pikepdf.Name("/Encoding"), BaseEncoding=base)
        font_dict["/Encoding"] = enc

    diffs = list(enc.get("/Differences") or [])

    remap: Dict[str, int] = {}
    for i, ch in enumerate(added):
        gid = new_gids[ch]
        code = free[i]
        gname = order[gid]
        diffs.extend([code, pikepdf.Name("/" + gname)])
        remap[ch] = code
        fi.widths[code] = hmtx.metrics[gname][0] * 1000.0 / sub_upem

    enc["/Differences"] = pikepdf.Array(diffs)
    new_codes.clear()
    new_codes.update(remap)

    # /Widths must cover FirstChar..LastChar with no gap
    codes = sorted(fi.widths.keys())
    first, last = codes[0], codes[-1]
    font_dict["/FirstChar"] = first
    font_dict["/LastChar"] = last
    font_dict["/Widths"] = pikepdf.Array(
        [round(fi.widths.get(c, fi.default_width), 2) for c in range(first, last + 1)])

    # a simple TrueType font goes through its cmap: we did not delete it, but
    # the glyph names require a usable post table.
    return True, None
