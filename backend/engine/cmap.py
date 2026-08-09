"""
Parsing and inversion of PDF CMaps (/ToUnicode) and of simple font encodings.

This is the most fundamental brick of the whole tool: a PDF does not store text,
it stores *codes* that index glyphs. To replace text you have to know how to make
both trips:

    code  -> unicode   (reading: what any extractor does)
    unicode -> code    (writing: what almost nobody does, and the only way to
                        rewrite in the original font)

The return trip is not free: it can be ambiguous (two codes for the same
character), partial (subsetted font: the glyph simply does not exist in the
file) or impossible (ligatures, exotic encodings).
We model that explicitly rather than guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .glyphlist import glyph_name_to_unicode
from .encodings import BASE_ENCODINGS

# --------------------------------------------------------------------------
# Minimal CMap tokenizer (PostScript subset)
# --------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    rb"""
      (?P<hex>\<[0-9A-Fa-f\s]*\>)      # <0041>
    | (?P<name>/[^\s/\[\]<>(){}%]+)    # /CIDInit
    | (?P<num>[-+]?\d*\.?\d+)          # 42
    | (?P<open>\[)
    | (?P<close>\])
    | (?P<op>[A-Za-z][A-Za-z0-9_*']*)  # begincmap, endbfrange...
    | (?P<comment>%[^\r\n]*)
    """,
    re.VERBOSE,
)


def _tokenize(data: bytes):
    for m in _TOKEN_RE.finditer(data):
        kind = m.lastgroup
        if kind == "comment":
            continue
        raw = m.group()
        if kind == "hex":
            h = re.sub(rb"\s", b"", raw[1:-1])
            if len(h) % 2:
                h += b"0"
            try:
                yield ("hex", bytes.fromhex(h.decode("ascii")))
            except ValueError:
                continue
        elif kind == "name":
            yield ("name", raw[1:].decode("latin-1"))
        elif kind == "num":
            yield ("num", raw.decode("ascii"))
        elif kind == "open":
            yield ("open", None)
        elif kind == "close":
            yield ("close", None)
        else:
            yield ("op", raw.decode("latin-1"))


def _bytes_to_int(b: bytes) -> int:
    return int.from_bytes(b, "big") if b else 0


def _utf16be_to_str(b: bytes) -> str:
    """The destinations of a bfchar/bfrange are UTF-16BE."""
    if not b:
        return ""
    if len(b) % 2:
        b = b + b"\x00"
    try:
        return b.decode("utf-16-be", errors="strict")
    except UnicodeDecodeError:
        # Lone surrogates / odd byte count: we tolerate it by replacing.
        return b.decode("utf-16-be", errors="replace")


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------


@dataclass
class CMap:
    """A parsed /ToUnicode CMap, in both directions."""

    # integer code -> unicode string (often 1 character, sometimes a ligature)
    to_unicode: Dict[int, str] = field(default_factory=dict)
    # unicode string -> list of the codes that produce it (ordered: the first
    # one is the preferred candidate for rewriting)
    from_unicode: Dict[str, List[int]] = field(default_factory=dict)
    # code lengths seen in the codespace (1 or 2 in practice)
    code_lengths: List[int] = field(default_factory=list)

    @property
    def code_len(self) -> int:
        if not self.code_lengths:
            return 1
        return max(self.code_lengths)

    def encode_char(self, ch: str) -> Optional[int]:
        codes = self.from_unicode.get(ch)
        return codes[0] if codes else None

    def build_reverse(self) -> None:
        rev: Dict[str, List[int]] = {}
        for code, s in self.to_unicode.items():
            if not s:
                continue
            rev.setdefault(s, []).append(code)
        for s in rev:
            rev[s].sort()
        self.from_unicode = rev


def parse_tounicode(data: bytes) -> CMap:
    """Parse the contents of a /ToUnicode stream."""
    cm = CMap()
    toks = list(_tokenize(data))
    i = 0
    n = len(toks)

    while i < n:
        kind, val = toks[i]

        if kind == "op" and val == "begincodespacerange":
            j = i + 1
            while j < n and not (toks[j][0] == "op" and toks[j][1] == "endcodespacerange"):
                if toks[j][0] == "hex" and j + 1 < n and toks[j + 1][0] == "hex":
                    cm.code_lengths.append(len(toks[j][1]))
                    j += 2
                else:
                    j += 1
            i = j + 1
            continue

        if kind == "op" and val == "beginbfchar":
            j = i + 1
            while j < n and not (toks[j][0] == "op" and toks[j][1] == "endbfchar"):
                if toks[j][0] == "hex" and j + 1 < n:
                    src = toks[j][1]
                    if toks[j + 1][0] == "hex":
                        cm.to_unicode[_bytes_to_int(src)] = _utf16be_to_str(toks[j + 1][1])
                        cm.code_lengths.append(len(src))
                        j += 2
                        continue
                    if toks[j + 1][0] == "name":
                        u = glyph_name_to_unicode(toks[j + 1][1])
                        if u:
                            cm.to_unicode[_bytes_to_int(src)] = u
                        j += 2
                        continue
                j += 1
            i = j + 1
            continue

        if kind == "op" and val == "beginbfrange":
            j = i + 1
            while j < n and not (toks[j][0] == "op" and toks[j][1] == "endbfrange"):
                if toks[j][0] == "hex" and j + 2 < n and toks[j + 1][0] == "hex":
                    lo_b, hi_b = toks[j][1], toks[j + 1][1]
                    lo, hi = _bytes_to_int(lo_b), _bytes_to_int(hi_b)
                    cm.code_lengths.append(len(lo_b))
                    nxt = toks[j + 2]

                    if nxt[0] == "hex":
                        # <lo> <hi> <dst>: increment the last code unit
                        dst = nxt[1]
                        base = _utf16be_to_str(dst)
                        span = min(hi - lo, 65535)
                        if len(base) == 1:
                            start = ord(base)
                            for k in range(span + 1):
                                cp = start + k
                                if 0 <= cp <= 0x10FFFF:
                                    cm.to_unicode[lo + k] = chr(cp)
                        elif base:
                            # multi-character destination: only the 1st code
                            # is really defined by the spec, we increment
                            # the last code unit
                            tail = ord(base[-1])
                            for k in range(span + 1):
                                cm.to_unicode[lo + k] = base[:-1] + chr(min(tail + k, 0x10FFFF))
                        j += 3
                        continue

                    if nxt[0] == "open":
                        # <lo> <hi> [ <d1> <d2> ... ]
                        k = j + 3
                        idx = 0
                        while k < n and toks[k][0] != "close":
                            if toks[k][0] == "hex":
                                cm.to_unicode[lo + idx] = _utf16be_to_str(toks[k][1])
                                idx += 1
                            elif toks[k][0] == "name":
                                u = glyph_name_to_unicode(toks[k][1])
                                if u:
                                    cm.to_unicode[lo + idx] = u
                                idx += 1
                            k += 1
                        j = k + 1
                        continue
                j += 1
            i = j + 1
            continue

        i += 1

    cm.code_lengths = sorted(set(x for x in cm.code_lengths if x in (1, 2, 3, 4)))
    cm.build_reverse()
    return cm


# --------------------------------------------------------------------------
# Simple font encoding (/Encoding + /Differences)
# --------------------------------------------------------------------------


def build_simple_encoding(
    base_encoding: Optional[str],
    differences: Optional[List],
    symbolic: bool = False,
    builtin: Optional[Dict[int, str]] = None,
) -> Tuple[Dict[int, str], Dict[int, str]]:
    """
    Build (code -> glyph name, code -> unicode) for a simple font.

    `builtin` = the font file's internal encoding (extracted by fontTools),
    used for symbolic fonts with no explicit /Encoding.
    """
    # Priority order imposed by ISO 32000-1 §9.6.6:
    #   explicit /BaseEncoding  >  the font's internal encoding  >  Standard
    # then /Differences on top, always.
    if base_encoding and base_encoding in BASE_ENCODINGS:
        names: Dict[int, str] = dict(BASE_ENCODINGS[base_encoding])
    elif builtin:
        names = dict(builtin)
    elif not symbolic:
        names = dict(BASE_ENCODINGS["StandardEncoding"])
    else:
        names = {}

    if differences:
        cur = 0
        for item in differences:
            if isinstance(item, (int, float)):
                cur = int(item)
            else:
                names[cur] = str(item)
                cur += 1

    uni: Dict[int, str] = {}
    for code, gname in names.items():
        u = glyph_name_to_unicode(gname)
        if u:
            uni[code] = u
    return names, uni
