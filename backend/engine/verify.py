"""
Verification by pixel comparison.

A promise of fidelity that nobody measures is not a promise. After surgery, we
render the original and the result under exactly the same conditions, we mask
the regions that are *supposed* to change, and we look at what is left.

What is left must be nothing. If it is not, we moved something we were not
supposed to move, and we say so instead of letting the user find out.

The result is a per-page integrity score, plus a readable difference image:
green = expected modification region, red = unexplained drift.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import fitz
import numpy as np

# Tolerance on the value of a pixel: the render engine's antialiasing is not
# perfectly deterministic from one file to the next (object order, matrix
# rounding). Below this, pixels are considered identical.
PIXEL_TOLERANCE = 12

# Dilation of the expected modification regions, in render pixels. A new glyph
# can spill over by a few pixels (descenders, accents).
MASK_PADDING = 4

# Below this number of pixels per million, the discrepancy is rasterisation
# noise and not a shift of content. On an A4 page rendered at 110 dpi, that
# amounts to fewer than thirty isolated pixels.
NEGLIGIBLE_PPM = 30.0


@dataclass
class PageVerdict:
    page: int
    width: int
    height: int
    total_pixels: int
    changed_pixels: int              # every pixel that differs
    expected_pixels: int             # those inside the modification regions
    unexpected_pixels: int           # those outside -> what must be zero
    max_delta_outside: int
    integrity: float                 # 1.0 = perfect
    boxes: List[List[float]] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return self.unexpected_pixels == 0

    @property
    def severity(self) -> str:
        """
        Three levels rather than a boolean.

        A render is not perfectly deterministic: shifting a piece of text by a
        thousandth of a point is enough to flip a few antialiasing pixels at
        the edge of a region. Mistaking those few pixels for a real shift of
        content would make the verdict useless on large documents — and above
        all, it would cry wolf.
        """
        if self.unexpected_pixels == 0:
            return "exact"
        ppm = self.unexpected_pixels / max(1, self.total_pixels) * 1e6
        if ppm < NEGLIGIBLE_PPM and self.unexpected_pixels < 400:
            return "negligible"
        return "drift"

    def to_dict(self) -> dict:
        return {
            "page": self.page,
            "severity": self.severity,
            "changed_pixels": self.changed_pixels,
            "expected_pixels": self.expected_pixels,
            "unexpected_pixels": self.unexpected_pixels,
            "max_delta_outside": int(self.max_delta_outside),
            "integrity": round(self.integrity, 6),
            "clean": self.clean,
            "ratio_ppm": round(self.unexpected_pixels / max(1, self.total_pixels) * 1e6, 2),
        }


@dataclass
class VerifyReport:
    pages: List[PageVerdict] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def clean(self) -> bool:
        return not self.error and all(p.clean for p in self.pages)

    @property
    def severity(self) -> str:
        if self.error:
            return "drift"
        levels = [p.severity for p in self.pages]
        if any(l == "drift" for l in levels):
            return "drift"
        if any(l == "negligible" for l in levels):
            return "negligible"
        return "exact"

    @property
    def acceptable(self) -> bool:
        """True as long as no content has moved perceptibly."""
        return self.severity != "drift"

    @property
    def integrity(self) -> float:
        if not self.pages:
            return 1.0
        return min(p.integrity for p in self.pages)

    @property
    def unexpected_total(self) -> int:
        return sum(p.unexpected_pixels for p in self.pages)

    def to_dict(self) -> dict:
        return {
            "clean": self.clean,
            "severity": self.severity,
            "acceptable": self.acceptable,
            "pages_drifting": sum(1 for p in self.pages if p.severity == "drift"),
            "integrity": round(self.integrity, 6),
            "unexpected_total": self.unexpected_total,
            "error": self.error,
            "pages": [p.to_dict() for p in self.pages],
        }


# --------------------------------------------------------------------------
# Blank box detection
# --------------------------------------------------------------------------


# Characters for which the absence of any drawing is perfectly normal.
_BLANKS = frozenset({0x20, 0x09, 0x0A, 0x0D, 0xA0, 0x200B, 0x200C, 0x200D,
                     0x2060, 0xFEFF, 0x00AD})


def find_notdef(pdf_bytes: bytes) -> List[dict]:
    """
    Lists the characters that produce no drawing at all — the blank boxes.

    This is the blind spot of pixel verification: an undrawn character appears
    *exactly* where a change is expected, hence inside the mask. It structurally
    cannot be seen by an image comparison. So a second proof is needed, of a
    different nature.

    Two signatures, because render engines do not all react the same way:

    - the glyph id is 0, that is `.notdef` by definition — some fonts draw a
      rectangle for it, hence the "blank box";
    - the id is positive but the glyph does not exist in the font: MuPDF then
      draws nothing at all and returns a zero-width box.

    The second case is the more insidious one: nothing on screen, no error, and
    the text stays perfectly extractable. That is exactly what happens when an
    embedded font declares more characters than it draws.

    Trap to avoid: a **negative** id signals the continuation of a ligature. An
    "fi" is a single glyph covering two characters, so the second one is
    legitimately without a drawing of its own. Counting those would make the
    detector scream on any TeX document — 556 false positives on the Nettle
    manual.
    """
    out: List[dict] = []
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return out
    try:
        for pno in range(doc.page_count):
            try:
                spans = doc[pno].get_texttrace() or []
            except Exception:
                continue
            for sp in spans:
                font = sp.get("font") or "?"
                for ch in sp.get("chars", ()):
                    try:
                        code, gid = int(ch[0]), int(ch[1])
                        bbox = list(ch[3]) if len(ch) > 3 and ch[3] else None
                    except Exception:
                        continue
                    if code in _BLANKS or gid < 0:
                        continue
                    blank = bbox is not None and (
                        abs(bbox[2] - bbox[0]) < 0.01 or abs(bbox[3] - bbox[1]) < 0.01)
                    if gid != 0 and not blank:
                        continue
                    out.append({
                        "page": pno, "font": font,
                        "char": chr(code) if 32 < code < 0x110000 else "?",
                        "gid": gid,
                        "cause": "notdef" if gid == 0 else "missing glyph",
                        "bbox": [round(v, 2) for v in bbox] if bbox else None,
                    })
    finally:
        doc.close()
    return out


def _key(e: dict) -> tuple:
    # Deliberately without coordinates: a replacement in "push" mode shifts
    # everything that follows on the line, and a pre-existing blank box would
    # then change position, hence key, and would pass for a new one.
    return (e["page"], e["char"], e["font"])


def new_notdef(before_bytes: bytes, after_bytes: bytes) -> List[dict]:
    """Blank boxes that *appeared*: many documents already contain some."""
    was: Dict[tuple, int] = {}
    for e in find_notdef(before_bytes):
        was[_key(e)] = was.get(_key(e), 0) + 1
    out: List[dict] = []
    seen: Dict[tuple, int] = {}
    for e in find_notdef(after_bytes):
        k = _key(e)
        seen[k] = seen.get(k, 0) + 1
        if seen[k] > was.get(k, 0):
            out.append(e)
    return out


def render_page(doc: fitz.Document, page_number: int, dpi: int = 110,
                gray: bool = False) -> np.ndarray:
    page = doc[page_number]
    cs = fitz.csGRAY if gray else fitz.csRGB
    pix = page.get_pixmap(dpi=dpi, colorspace=cs, alpha=False, annots=True)
    arr = np.frombuffer(pix.samples, dtype=np.uint8)
    return arr.reshape(pix.height, pix.width, pix.n)


def render_png(doc: fitz.Document, page_number: int, dpi: int = 110) -> bytes:
    page = doc[page_number]
    return page.get_pixmap(dpi=dpi, alpha=False, annots=True).tobytes("png")


def _dilate(mask: np.ndarray, radius: int) -> np.ndarray:
    """Separable binary dilation, with no dependency on scipy."""
    if radius <= 0:
        return mask
    out = mask.copy()
    for _ in range(radius):
        shifted = out.copy()
        shifted[1:, :] |= out[:-1, :]
        shifted[:-1, :] |= out[1:, :]
        shifted[:, 1:] |= out[:, :-1]
        shifted[:, :-1] |= out[:, 1:]
        out = shifted
    return out


def _boxes_to_mask(boxes: Sequence[Sequence[float]], shape: Tuple[int, int],
                   sx: float, sy: float) -> np.ndarray:
    """
    Rasterises the expected regions.

    The scale is derived from the real dimensions of the image, not from
    dpi/72: the pixmap is rounded to the integer, and that lost fraction of a
    pixel was enough to push a few border pixels out of the mask at certain
    resolutions — a false drift positive, very confusing on a report that
    promises to be exact.
    """
    h, w = shape
    mask = np.zeros((h, w), dtype=bool)
    for b in boxes:
        x0, x1 = b[0] * sx, b[2] * sx
        y0, y1 = b[1] * sy, b[3] * sy
        i0 = max(0, int(np.floor(y0)) - MASK_PADDING)
        i1 = min(h, int(np.ceil(y1)) + MASK_PADDING + 1)
        j0 = max(0, int(np.floor(x0)) - MASK_PADDING)
        j1 = min(w, int(np.ceil(x1)) + MASK_PADDING + 1)
        if i1 > i0 and j1 > j0:
            mask[i0:i1, j0:j1] = True
    return mask


def _page_scale(doc: fitz.Document, page_number: int,
                shape: Tuple[int, int]) -> Tuple[float, float]:
    try:
        r = doc[page_number].rect
        if r.width > 0 and r.height > 0:
            return shape[1] / r.width, shape[0] / r.height
    except Exception:
        pass
    return 1.0, 1.0


def search_rects(page, needle: str) -> List[List[float]]:
    """
    Rectangles of a string on a page, expressed in the space of the *render*.

    A subtlety that costs dearly if ignored: on a rotated page, `search_for`
    answers in the unrotated frame (the MediaBox) whereas `get_pixmap` draws in
    the rotated frame. Without the rotation matrix, the verification mask and
    the highlights land somewhere else — and the integrity check reports a
    drift that does not exist.
    """
    out: List[List[float]] = []
    try:
        rects = page.search_for(needle, quads=False)
    except Exception:
        return out
    try:
        m = page.rotation_matrix
    except Exception:
        m = None
    for r in rects:
        rr = (r * m) if m is not None else r
        out.append([min(rr.x0, rr.x1), min(rr.y0, rr.y1),
                    max(rr.x0, rr.x1), max(rr.y0, rr.y1)])
    return out


def collect_boxes(doc: fitz.Document, page_number: int,
                  needles: Sequence[str]) -> List[List[float]]:
    """Rectangles of the searched strings on a page, in render points."""
    out: List[List[float]] = []
    try:
        page = doc[page_number]
    except Exception:
        return out
    seen = set()
    for needle in needles:
        n = (needle or "").strip()
        if not n or n in seen:
            continue
        seen.add(n)
        out.extend(search_rects(page, n))
    return out


def compare_pages(before: fitz.Document, after: fitz.Document,
                  page_number: int, boxes_pt: Sequence[Sequence[float]],
                  dpi: int = 110) -> PageVerdict:
    a = render_page(before, page_number, dpi=dpi, gray=True)[:, :, 0].astype(np.int16)
    b = render_page(after, page_number, dpi=dpi, gray=True)[:, :, 0].astype(np.int16)

    if a.shape != b.shape:
        h = min(a.shape[0], b.shape[0])
        w = min(a.shape[1], b.shape[1])
        a, b = a[:h, :w], b[:h, :w]

    diff = np.abs(a - b)
    changed = diff > PIXEL_TOLERANCE

    sx, sy = _page_scale(before, page_number, a.shape)
    mask = _dilate(_boxes_to_mask(boxes_pt, a.shape, sx, sy), 1)

    inside = changed & mask
    outside = changed & ~mask

    n_out = int(outside.sum())
    total = int(a.size)
    max_out = int(diff[outside].max()) if n_out else 0

    return PageVerdict(
        page=page_number,
        width=a.shape[1], height=a.shape[0], total_pixels=total,
        changed_pixels=int(changed.sum()),
        expected_pixels=int(inside.sum()),
        unexpected_pixels=n_out,
        max_delta_outside=max_out,
        integrity=1.0 - (n_out / total),
        boxes=[list(b) for b in boxes_pt],
    )


def diff_image(before: fitz.Document, after: fitz.Document, page_number: int,
               boxes_pt: Sequence[Sequence[float]], dpi: int = 110) -> bytes:
    """
    Control image: the document in pale grey, the expected modifications in
    green, any unexpected drift in bright red.
    """
    a = render_page(before, page_number, dpi=dpi, gray=True)[:, :, 0].astype(np.int16)
    b = render_page(after, page_number, dpi=dpi, gray=True)[:, :, 0].astype(np.int16)
    if a.shape != b.shape:
        h = min(a.shape[0], b.shape[0]); w = min(a.shape[1], b.shape[1])
        a, b = a[:h, :w], b[:h, :w]

    diff = np.abs(a - b)
    changed = diff > PIXEL_TOLERANCE
    sx, sy = _page_scale(before, page_number, a.shape)
    mask = _dilate(_boxes_to_mask(boxes_pt, a.shape, sx, sy), 1)

    # background: the result, lightened so the highlights stand out
    base = (b * 0.35 + 255 * 0.65).astype(np.uint8)
    rgb = np.dstack([base, base, base])

    inside = _dilate(changed & mask, 1)
    outside = _dilate(changed & ~mask, 2)

    rgb[inside] = np.array([16, 160, 88], dtype=np.uint8)
    rgb[outside] = np.array([220, 38, 38], dtype=np.uint8)

    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(rgb, mode="RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def verify_documents(before_bytes: bytes, after_bytes: bytes,
                     page_boxes: Dict[int, List[List[float]]],
                     dpi: int = 110) -> VerifyReport:
    """Compare two documents page by page, on the pages actually touched."""
    report = VerifyReport()
    try:
        before = fitz.open(stream=before_bytes, filetype="pdf")
        after = fitz.open(stream=after_bytes, filetype="pdf")
    except Exception as e:
        report.error = "cannot open: %s" % e
        return report

    try:
        if before.page_count != after.page_count:
            report.error = "the page count changed (%d -> %d)" % (
                before.page_count, after.page_count)
            return report
        for pno in sorted(page_boxes.keys()):
            if not 0 <= pno < before.page_count:
                continue
            try:
                report.pages.append(
                    compare_pages(before, after, pno, page_boxes[pno], dpi=dpi))
            except Exception as e:
                report.error = "page %d: %s" % (pno + 1, e)
    finally:
        before.close()
        after.close()
    return report


def new_outside_page(before_bytes: bytes, after_bytes: bytes) -> int:
    """Count newly clipped visible glyphs, including those hidden by the crop.

    Pixel masks cannot detect text drawn outside the page. Compare glyph
    traces independently, ignoring clipping already present in the source.
    """
    from collections import Counter

    def clipped(raw):
        counts = Counter()
        with fitz.open(stream=raw, filetype="pdf") as doc:
            for pno, page in enumerate(doc):
                bounds = page.rect * page.derotation_matrix
                for span in page.get_texttrace():
                    if span.get("type") == 3 or span.get("opacity", 1) == 0:
                        continue
                    for cp, gid, origin, box in span["chars"]:
                        if cp <= 32 or gid <= 0:
                            continue
                        if (box[0] < bounds.x0 - .5 or box[1] < bounds.y0 - .5
                                or box[2] > bounds.x1 + .5 or box[3] > bounds.y1 + .5):
                            counts[(pno, cp, span.get("font", ""))] += 1
        return counts

    return sum((clipped(after_bytes) - clipped(before_bytes)).values())
