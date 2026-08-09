"""
Glyph name -> Unicode, through the Adobe Glyph List (shipped by fontTools) plus
the standard fallback algorithm (uniXXXX, uXXXXXX, .sc/.alt suffixes, gXX/cidXX).
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Dict, Optional

from fontTools import agl

_UNI_RE = re.compile(r"^uni([0-9A-Fa-f]{4})+$")
_U_RE = re.compile(r"^u([0-9A-Fa-f]{4,6})$")
_NUMERIC_RE = re.compile(r"^(?:g|G|glyph|cid|c|index)(\d+)$")


@lru_cache(maxsize=8192)
def glyph_name_to_unicode(name: str) -> Optional[str]:
    """Return the Unicode string for a glyph name, or None if undeterminable."""
    if not name:
        return None
    if name in (".notdef", ".null", "nonmarkingreturn"):
        return None

    # 1. direct AGL hit
    uv = agl.AGL2UV.get(name)
    if uv is not None:
        return chr(uv)

    # 2. uniXXXX (possibly repeated = ligature)
    m = _UNI_RE.match(name)
    if m:
        parts = [name[i : i + 4] for i in range(3, len(name), 4)]
        try:
            return "".join(chr(int(p, 16)) for p in parts)
        except ValueError:
            return None

    # 3. uXXXX..XXXXXX
    m = _U_RE.match(name)
    if m:
        try:
            cp = int(m.group(1), 16)
            if 0 <= cp <= 0x10FFFF:
                return chr(cp)
        except ValueError:
            return None

    # 4. suffixes: a.sc, one.oldstyle, f_i.liga -> retry on the base name
    if "." in name:
        base = name.split(".", 1)[0]
        if base and base != name:
            return glyph_name_to_unicode(base)

    # 5. ligatures named with underscores: f_i, ff_i
    if "_" in name:
        out = []
        for part in name.split("_"):
            u = glyph_name_to_unicode(part)
            if not u:
                return None
            out.append(u)
        return "".join(out)

    # 6. gXX / cidXX: purely positional, no Unicode meaning
    if _NUMERIC_RE.match(name):
        return None

    # 7. last chance: fontTools.agl.toUnicode handles a few exotic cases
    try:
        u = agl.toUnicode(name)
        return u or None
    except Exception:
        return None


@lru_cache(maxsize=1)
def _reverse_agl() -> Dict[str, str]:
    rev: Dict[str, str] = {}
    for gname, uv in agl.AGL2UV.items():
        ch = chr(uv)
        # prefer short names with no suffix (a rather than afii10017)
        prev = rev.get(ch)
        if prev is None or (len(gname), gname) < (len(prev), prev):
            rev[ch] = gname
    return rev


def unicode_to_glyph_name(ch: str) -> Optional[str]:
    """Canonical glyph name for a character (to build /Differences)."""
    if len(ch) != 1:
        return None
    name = _reverse_agl().get(ch)
    if name:
        return name
    cp = ord(ch)
    return "uni%04X" % cp if cp <= 0xFFFF else "u%06X" % cp
