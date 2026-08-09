"""
The base encodings predefined by the PDF spec (ISO 32000-1, annex D).

Each table is code (0-255) -> glyph name. StandardEncoding and MacRoman come
from fontTools; WinAnsi and PDFDoc are derived from cp1252 through the reverse
AGL, with the spec's explicit deviations applied afterwards.
"""

from __future__ import annotations

from typing import Dict

from fontTools.encodings.MacRoman import MacRoman
from fontTools.encodings.StandardEncoding import StandardEncoding

from .glyphlist import unicode_to_glyph_name


def _from_list(lst) -> Dict[int, str]:
    out: Dict[int, str] = {}
    for code, name in enumerate(lst):
        if name and name != ".notdef":
            out[code] = name
    return out


def _winansi() -> Dict[int, str]:
    out: Dict[int, str] = {}
    for code in range(32, 256):
        try:
            ch = bytes([code]).decode("cp1252")
        except UnicodeDecodeError:
            continue
        name = unicode_to_glyph_name(ch)
        if name:
            out[code] = name
    # Deviations from strict cp1252 imposed by the PDF spec.
    out[0xA0] = "space"      # NBSP rendered as a space
    out[0xAD] = "hyphen"     # soft hyphen rendered as a hyphen
    # cp1252 leaves 0x81/0x8D/0x8F/0x90/0x9D undefined: the spec does not
    # define them either, so we leave them out.
    return out


def _pdfdoc() -> Dict[int, str]:
    # PDFDocEncoding is not used to render a page, only for metadata strings.
    # We include it to read /Title, /Author, etc. properly.
    out = dict(_winansi())
    for code in range(0x18, 0x20):
        out.pop(code, None)
    return out


BASE_ENCODINGS: Dict[str, Dict[int, str]] = {
    "StandardEncoding": _from_list(StandardEncoding),
    "MacRomanEncoding": _from_list(MacRoman),
    "WinAnsiEncoding": _winansi(),
    "PDFDocEncoding": _pdfdoc(),
    # MacExpertEncoding is an expert glyph encoding (small caps, oldstyle
    # figures). Extremely rare and never carries editable dates: we map it
    # onto Standard so nothing crashes.
    "MacExpertEncoding": _from_list(StandardEncoding),
}

# The 14 standard PostScript fonts: no mandatory /Widths, implicit metrics.
# We do not treat them specially for advance widths (MuPDF hands those to us),
# but we do want to recognize them so we can tell the user the font is not
# embedded.
STANDARD_14 = {
    "Times-Roman", "Times-Bold", "Times-Italic", "Times-BoldItalic",
    "Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique",
    "Courier", "Courier-Bold", "Courier-Oblique", "Courier-BoldOblique",
    "Symbol", "ZapfDingbats",
    # common aliases
    "Arial", "Arial-Bold", "Arial,Bold", "ArialMT", "Arial-BoldMT",
    "TimesNewRoman", "TimesNewRomanPSMT", "CourierNew", "CourierNewPSMT",
}
