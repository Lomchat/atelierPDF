"""
HTTP API of the PDF editing platform.

The unit of work is a **batch**: a set of documents to which the same rules are
applied. A batch of a single document remains the common case, but nothing in
the contract assumes it.

Contract: the client only ever holds ids. The date format, the font encoding,
the position in the stream — everything is recomputed server-side from the
PDFs, which are the only source of truth.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import shutil
import sys
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import fitz
from dotenv import load_dotenv
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from starlette.exceptions import HTTPException as StarletteHTTPException
from audit import AuditStore, install_admin, install_audit
from public_api import install_public_api
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import (FileResponse, JSONResponse, PlainTextResponse,
                               Response)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from engine.document import analyze, drop_cached, process
from engine.fontrepair import FontLibrary
from engine.search import Finder as EngineFinder
from engine.verify import diff_image

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))
DATA_DIR = os.environ.get("ATELIER_DATA_DIR", os.path.join(BASE_DIR, "data"))
LOTS_DIR = os.path.join(DATA_DIR, "sessions")
FONTS_DIR = os.path.join(DATA_DIR, "fonts")
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")
LOG_PATH = os.path.join(DATA_DIR, "usage.log")

MAX_FILE_BYTES = 40 * 1024 * 1024
MAX_LOT_BYTES = 160 * 1024 * 1024
MAX_DOCS = 24
MAX_PAGES = 800
SESSION_TTL = 3 * 3600
CLEANUP_INTERVAL = 600

# The documents of a batch are processed in parallel. The engine spends most of
# its time in pikepdf and MuPDF, which release the GIL: threads are enough.
MAX_PARALLEL = 4

for d in (DATA_DIR, LOTS_DIR, FONTS_DIR):
    os.makedirs(d, exist_ok=True)

app = FastAPI(title="Atelier PDF", version="1.1.0",
              description="API publique de remplacement de texte dans les PDF. Commencez par POST /api/replace.",
              openapi_tags=[{"name": "API publique"}, {"name": "default", "description": "Parcours interactif par sessions utilisé par la plateforme."}],
              docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None,
              swagger_ui_parameters={"defaultModelsExpandDepth": -1, "displayRequestDuration": True})
app.add_middleware(GZipMiddleware, minimum_size=1024)

_font_library = FontLibrary(extra_dirs=[FONTS_DIR])
_sem = asyncio.Semaphore(MAX_PARALLEL)


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------


@dataclass
class Doc:
    did: str
    filename: str
    size: int
    pages: int = 0
    analysis: Optional[dict] = None
    last_outcome: Optional[dict] = None
    has_output: bool = False
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.did, "filename": self.filename, "size": self.size,
            "pages": self.pages, "analysis": self.analysis,
            "last_outcome": self.last_outcome, "has_output": self.has_output,
            "error": self.error,
        }


@dataclass
class Lot:
    """
    A batch of documents sharing the same rules.

    Written to disk on every mutation: the service runs with several uvicorn
    processes that share no memory, and a restart must not throw away the work
    in progress. The in-memory cache only saves the re-read.
    """

    sid: str
    created: float
    docs: List[Doc] = field(default_factory=list)
    extra_fonts: Dict[str, str] = field(default_factory=dict)
    seq: int = 0
    # Date of the file at the moment we read it. The service runs with several
    # processes: the one that applies is not necessarily the one that serves
    # the download, and its in-memory copy would say nothing of the changes.
    mtime: float = 0.0

    # -- paths -----------------------------------------------------------

    @property
    def dir(self) -> str:
        return os.path.join(LOTS_DIR, self.sid)

    @property
    def meta_path(self) -> str:
        return os.path.join(self.dir, "meta.json")

    def doc_dir(self, did: str) -> str:
        return os.path.join(self.dir, did)

    def original(self, did: str) -> str:
        return os.path.join(self.doc_dir(did), "original.pdf")

    def edited(self, did: str) -> str:
        return os.path.join(self.doc_dir(did), "edited.pdf")

    def render_path(self, did: str, version: str, page: int, dpi: int) -> str:
        return os.path.join(self.doc_dir(did), "r_%s_%d_%d.png" % (version, page, dpi))

    # -- access ----------------------------------------------------------

    def doc(self, did: str) -> Doc:
        for d in self.docs:
            if d.did == did:
                return d
        raise HTTPException(404, "Unknown document in this batch.")

    def read(self, did: str, version: str) -> bytes:
        d = self.doc(did)
        path = self.edited(did) if (version == "after" and d.has_output) \
            else self.original(did)
        with open(path, "rb") as fh:
            return fh.read()

    @property
    def total_bytes(self) -> int:
        return sum(d.size for d in self.docs)

    def cache_key(self, did: str) -> str:
        return "%s:%s" % (self.sid, did)

    # -- persistence -----------------------------------------------------

    def save(self) -> None:
        tmp = self.meta_path + ".tmp"
        payload = {
            "sid": self.sid, "created": self.created, "seq": self.seq,
            "extra_fonts": self.extra_fonts,
            "docs": [d.to_dict() for d in self.docs],
        }
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, self.meta_path)
        try:
            self.mtime = os.path.getmtime(self.meta_path)
        except OSError:
            self.mtime = time.time()

    @classmethod
    def load(cls, sid: str) -> Optional["Lot"]:
        try:
            with open(os.path.join(LOTS_DIR, sid, "meta.json"), encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            return None
        lot = cls(sid=d["sid"], created=d.get("created", time.time()),
                  extra_fonts=d.get("extra_fonts") or {}, seq=int(d.get("seq", 0)))
        try:
            lot.mtime = os.path.getmtime(os.path.join(LOTS_DIR, sid, "meta.json"))
        except OSError:
            lot.mtime = 0.0
        for x in d.get("docs", []):
            lot.docs.append(Doc(
                did=x["id"], filename=x.get("filename", "document.pdf"),
                size=int(x.get("size", 0)), pages=int(x.get("pages", 0)),
                analysis=x.get("analysis"), last_outcome=x.get("last_outcome"),
                has_output=bool(x.get("has_output")), error=x.get("error")))
        return lot

    def to_dict(self) -> dict:
        return {"id": self.sid, "docs": [d.to_dict() for d in self.docs]}


LOTS: Dict[str, Lot] = {}

_SID_RE = re.compile(r"^[0-9a-f]{8,40}$")
_DID_RE = re.compile(r"^d[0-9]{1,4}$")


def get_lot(sid: str) -> Lot:
    if not _SID_RE.match(sid or ""):
        raise HTTPException(400, "Invalid batch id.")
    lot = LOTS.get(sid)
    if lot is not None:
        # Has another process touched this batch since we read it?
        try:
            if os.path.getmtime(lot.meta_path) > lot.mtime + 1e-6:
                lot = None
        except OSError:
            lot = None
    if lot is None:
        lot = Lot.load(sid)
        if lot is not None:
            LOTS[sid] = lot
    if lot is None or not os.path.isdir(lot.dir):
        raise HTTPException(404, "Unknown or expired batch. Drop your files again.")
    return lot


def check_did(did: str) -> str:
    if not _DID_RE.match(did or ""):
        raise HTTPException(400, "Invalid document id.")
    return did


def _safe_name(name: str) -> str:
    name = os.path.basename(name or "document.pdf")
    name = re.sub(r"[^\w\s.,\-()À-ÿ]", "_", name).strip() or "document.pdf"
    return name[:120]


def _output_name(filename: str) -> str:
    base, ext = os.path.splitext(filename)
    return "%s_1%s" % (base, ext or ".pdf")


def _log(request: Request, event: str, **fields) -> None:
    """Add metadata to the single audit record for this request."""
    if hasattr(request.state, "audit"):
        request.state.audit.update(event=event, **fields)


_audit_store = AuditStore(os.path.join(DATA_DIR, "operations.sqlite3"), LOG_PATH)
install_audit(app, _audit_store)
install_admin(app, _audit_store)


@app.exception_handler(StarletteHTTPException)
async def logged_http_error(request: Request, exc):
    if hasattr(request.state, "audit"):
        request.state.audit["error"] = exc.detail
    return await http_exception_handler(request, exc)


@app.exception_handler(RequestValidationError)
async def logged_validation_error(request: Request, exc):
    if hasattr(request.state, "audit"):
        request.state.audit["error"] = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
    return await request_validation_exception_handler(request, exc)


async def _cleanup_loop() -> None:
    while True:
        try:
            now = time.time()
            for sid, lot in list(LOTS.items()):
                if now - lot.created > SESSION_TTL:
                    _drop(sid, lot)
            if os.path.isdir(LOTS_DIR):
                for name in os.listdir(LOTS_DIR):
                    p = os.path.join(LOTS_DIR, name)
                    try:
                        if os.path.isdir(p) and now - os.path.getmtime(p) > SESSION_TTL:
                            shutil.rmtree(p, ignore_errors=True)
                            LOTS.pop(name, None)
                    except OSError:
                        pass
        except Exception:
            pass
        await asyncio.sleep(CLEANUP_INTERVAL)


def _drop(sid: str, lot: Optional[Lot] = None) -> None:
    lot = lot or LOTS.get(sid)
    if lot is not None:
        for d in lot.docs:
            drop_cached(lot.cache_key(d.did))
        shutil.rmtree(lot.dir, ignore_errors=True)
    LOTS.pop(sid, None)


@app.on_event("startup")
async def _startup() -> None:
    asyncio.get_event_loop().create_task(_cleanup_loop())
    await asyncio.get_event_loop().run_in_executor(None, _font_library.scan)


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------


class DateRule(BaseModel):
    type: str = "shift"
    days: int = 0
    months: int = 0
    years: int = 0
    iso: Optional[str] = None
    value: Optional[str] = None
    pairs: Dict[str, str] = Field(default_factory=dict)
    day: Optional[int] = None
    month: Optional[int] = None
    year: Optional[int] = None


class FinderIn(BaseModel):
    id: str
    kind: str = "text"              # text | regex | dates
    query: str = ""
    replace: str = ""
    case_sensitive: bool = False
    whole_word: bool = False
    loose: bool = True
    enabled: bool = True

    def to_engine(self) -> EngineFinder:
        return EngineFinder(
            id=self.id, kind=self.kind, query=self.query, replace=self.replace,
            case_sensitive=self.case_sensitive, whole_word=self.whole_word,
            loose=self.loose, enabled=self.enabled)


class DocTweaks(BaseModel):
    """Fine-grained settings specific to one document of the batch."""

    overrides: Dict[str, str] = Field(default_factory=dict)
    fits: Dict[str, str] = Field(default_factory=dict)
    anchors: Dict[str, str] = Field(default_factory=dict)
    # Denylist of stable identities — never an allowlist, see
    # engine.document.process.
    exclude: List[str] = Field(default_factory=list)


class EditRequest(BaseModel):
    finders: List[FinderIn] = Field(default_factory=list)
    date_rule: DateRule = Field(default_factory=DateRule)
    per_doc: Dict[str, DocTweaks] = Field(default_factory=dict)
    only: Optional[List[str]] = None      # restrict to certain documents
    locale: str = "fr"
    auto_repair: bool = True
    verify: bool = True

    def engine_finders(self) -> List[EngineFinder]:
        return [f.to_engine() for f in self.finders[:24]]

    def tweaks(self, did: str) -> DocTweaks:
        return self.per_doc.get(did) or DocTweaks()


# --------------------------------------------------------------------------
# Processing a batch
# --------------------------------------------------------------------------


async def _run(fn, *a, **kw):
    """A heavy job, in a thread, with a cap on parallelism."""
    async with _sem:
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: fn(*a, **kw))


async def _for_each_doc(lot: Lot, req: EditRequest, fn) -> List[dict]:
    """Applies `fn(doc)` to all the targeted documents, in parallel."""
    only = set(req.only) if req.only else None
    targets = [d for d in lot.docs if d.error is None and (only is None or d.did in only)]
    results = await asyncio.gather(*[fn(d) for d in targets], return_exceptions=True)

    out: List[dict] = []
    for doc, res in zip(targets, results):
        if isinstance(res, Exception):
            out.append({"id": doc.did, "error": str(res)})
        else:
            out.append(res)
    return out


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "lots": len(LOTS), "fonts_indexed": len(_font_library.entries)}


async def _ingest(lot: Lot, files: List[UploadFile], locale: str) -> List[Doc]:
    """Stores new files and analyses them structurally."""
    added: List[Doc] = []

    for up in files:
        if len(lot.docs) >= MAX_DOCS:
            raise HTTPException(413, "Maximum %d documents per batch." % MAX_DOCS)
        raw = await up.read(MAX_FILE_BYTES + 1)
        name = _safe_name(up.filename)
        if not raw:
            continue
        if len(raw) > MAX_FILE_BYTES:
            raise HTTPException(413, '"%s" is over %d MB.'
                                % (name, MAX_FILE_BYTES // (1024 * 1024)))
        if lot.total_bytes + len(raw) > MAX_LOT_BYTES:
            raise HTTPException(413, "The batch is over %d MB in total."
                                % (MAX_LOT_BYTES // (1024 * 1024)))
        if raw[:5] != b"%PDF-":
            raise HTTPException(400, '"%s" is not a PDF.' % name)

        did = "d%d" % lot.seq
        lot.seq += 1
        os.makedirs(lot.doc_dir(did), exist_ok=True)
        with open(lot.original(did), "wb") as fh:
            fh.write(raw)

        doc = Doc(did=did, filename=name, size=len(raw))
        try:
            analysis = await _run(analyze, raw, [], None, locale,
                                  cache_key=lot.cache_key(did))
            if analysis.pages > MAX_PAGES:
                raise ValueError("document too long (%d pages, maximum %d)"
                                 % (analysis.pages, MAX_PAGES))
            doc.analysis = analysis.to_dict()
            doc.pages = analysis.pages
        except Exception as e:
            doc.error = str(e)
        lot.docs.append(doc)
        added.append(doc)

    lot.save()
    return added


@app.post("/api/lots")
async def create_lot(request: Request, files: List[UploadFile] = File(...),
                     locale: str = Form("fr")) -> JSONResponse:
    _log(request, "upload", names=[_safe_name(f.filename) for f in files])
    if not files:
        raise HTTPException(400, "No file.")
    sid = uuid.uuid4().hex[:20]
    lot = Lot(sid=sid, created=time.time())
    os.makedirs(lot.dir, exist_ok=True)
    try:
        await _ingest(lot, files, locale)
    except HTTPException:
        shutil.rmtree(lot.dir, ignore_errors=True)
        raise
    if not lot.docs:
        shutil.rmtree(lot.dir, ignore_errors=True)
        raise HTTPException(400, "No usable document.")

    LOTS[sid] = lot
    _log(request, "upload", sid=sid, docs=len(lot.docs), bytes=lot.total_bytes,
         names=[d.filename for d in lot.docs], files=[{"filename": d.filename, "size": d.size, "pages": d.pages, "error": d.error} for d in lot.docs])
    return JSONResponse(lot.to_dict())


@app.post("/api/lots/{sid}/documents")
async def add_documents(request: Request, sid: str,
                        files: List[UploadFile] = File(...),
                        locale: str = Form("fr")) -> JSONResponse:
    lot = get_lot(sid)
    _log(request, "add", sid=sid, names=[_safe_name(f.filename) for f in files])
    added = await _ingest(lot, files, locale)
    _log(request, "add", sid=sid, docs=len(added), files=[{"filename": d.filename, "size": d.size, "pages": d.pages, "error": d.error} for d in added])
    return JSONResponse({"id": sid, "added": [d.did for d in added],
                         "docs": [d.to_dict() for d in lot.docs]})


@app.delete("/api/lots/{sid}/documents/{did}")
async def remove_document(request: Request, sid: str, did: str) -> dict:
    lot = get_lot(sid)
    check_did(did)
    doc = lot.doc(did)
    _log(request, "remove_document", sid=sid, names=[doc.filename])
    drop_cached(lot.cache_key(did))
    shutil.rmtree(lot.doc_dir(did), ignore_errors=True)
    lot.docs = [d for d in lot.docs if d.did != did]
    lot.save()
    return {"ok": True, "docs": [d.to_dict() for d in lot.docs]}


@app.get("/api/lots/{sid}")
async def get_lot_state(sid: str) -> dict:
    return get_lot(sid).to_dict()


@app.delete("/api/lots/{sid}")
async def drop_lot(sid: str) -> dict:
    _drop(sid)
    return {"ok": True}


@app.post("/api/lots/{sid}/scan")
async def scan(request: Request, sid: str, req: EditRequest) -> JSONResponse:
    """Applies the search rules to the whole batch."""
    request.state.audit["rules"] = req.model_dump()
    lot = get_lot(sid)
    finders = req.engine_finders()
    rule = req.date_rule.dict(exclude_none=False)

    async def one(doc: Doc) -> dict:
        raw = lot.read(doc.did, "before")
        analysis = await _run(analyze, raw, finders, rule, req.locale,
                              cache_key=lot.cache_key(doc.did))
        doc.analysis = analysis.to_dict()
        return {"id": doc.did, "analysis": doc.analysis}

    out = await _for_each_doc(lot, req, one)
    request.state.audit["documents"] = [{"id": d["id"], "error": d.get("error"), "matches": len((d.get("analysis") or {}).get("occurrences", [])), "finder_errors": (d.get("analysis") or {}).get("finder_errors", {})} for d in out]
    lot.save()
    return JSONResponse({"id": sid, "docs": out})


def _process_args(lot: Lot, doc: Doc, req: EditRequest, dry: bool) -> dict:
    t = req.tweaks(doc.did)
    return dict(
        finders=req.engine_finders(),
        date_rule=req.date_rule.dict(exclude_none=False),
        overrides=t.overrides, exclude=t.exclude, fits=t.fits, anchors=t.anchors,
        locale_hint=req.locale, dry_run=dry,
        auto_repair=(False if dry else req.auto_repair),
        verify=(False if dry else req.verify),
        font_library=_font_library, extra_font_paths=lot.extra_fonts,
        cache_key=(lot.cache_key(doc.did) if dry else None),
    )


@app.post("/api/lots/{sid}/simulate")
async def simulate(request: Request, sid: str, req: EditRequest) -> JSONResponse:
    request.state.audit["rules"] = req.model_dump()
    lot = get_lot(sid)

    async def one(doc: Doc) -> dict:
        raw = lot.read(doc.did, "before")
        outcome = await _run(process, raw, **_process_args(lot, doc, req, True))
        return {"id": doc.did, "outcome": outcome.to_dict()}

    out = await _for_each_doc(lot, req, one)
    request.state.audit["documents"] = out
    return JSONResponse({"id": sid, "docs": out})


@app.post("/api/lots/{sid}/apply")
async def apply_lot(request: Request, sid: str, req: EditRequest) -> JSONResponse:
    request.state.audit["rules"] = req.model_dump()
    lot = get_lot(sid)

    async def one(doc: Doc) -> dict:
        raw = lot.read(doc.did, "before")
        outcome = await _run(process, raw, **_process_args(lot, doc, req, False))
        if outcome.pdf_bytes and outcome.applied:
            with open(lot.edited(doc.did), "wb") as fh:
                fh.write(outcome.pdf_bytes)
            doc.has_output = True
        else:
            # No replacement: we do not manufacture a useless duplicate.
            doc.has_output = False
            try:
                os.remove(lot.edited(doc.did))
            except OSError:
                pass
        doc.last_outcome = outcome.to_dict()
        doc.last_outcome["output_size"] = len(outcome.pdf_bytes or b"")

        for name in os.listdir(lot.doc_dir(doc.did)):
            if name.startswith("r_after_") or name.startswith("r_diff_"):
                try:
                    os.remove(os.path.join(lot.doc_dir(doc.did), name))
                except OSError:
                    pass
        return {"id": doc.did, "outcome": doc.last_outcome}

    out = await _for_each_doc(lot, req, one)
    lot.save()

    applied = sum((d.get("outcome") or {}).get("applied", 0) for d in out)
    request.state.audit["documents"] = out
    _log(request, "apply", sid=sid, docs=len(out), applied=applied,
         modified=sum(1 for d in lot.docs if d.has_output))
    return JSONResponse({"id": sid, "docs": out,
                         "modified": [d.did for d in lot.docs if d.has_output]})


# --------------------------------------------------------------------------
# Renders and downloads
# --------------------------------------------------------------------------


@app.get("/api/lots/{sid}/documents/{did}/page/{page}.png")
async def page_png(sid: str, did: str, page: int, v: str = "before",
                   dpi: int = 130) -> Response:
    lot = get_lot(sid)
    check_did(did)
    doc = lot.doc(did)
    dpi = max(60, min(300, dpi))
    version = "after" if (v == "after" and doc.has_output) else "before"

    cached = lot.render_path(did, version, page, dpi)
    if os.path.exists(cached):
        return FileResponse(cached, media_type="image/png",
                            headers={"Cache-Control": "private, max-age=600"})

    def render() -> bytes:
        data = lot.read(did, version)
        d = fitz.open(stream=data, filetype="pdf")
        try:
            if not 0 <= page < d.page_count:
                raise HTTPException(404, "No such page.")
            return d[page].get_pixmap(dpi=dpi, alpha=False).tobytes("png")
        finally:
            d.close()

    png = await _run(render)
    try:
        with open(cached, "wb") as fh:
            fh.write(png)
    except OSError:
        pass
    return Response(png, media_type="image/png",
                    headers={"Cache-Control": "private, max-age=600"})


@app.get("/api/lots/{sid}/documents/{did}/diff/{page}.png")
async def diff_png(sid: str, did: str, page: int, dpi: int = 130) -> Response:
    lot = get_lot(sid)
    check_did(did)
    doc = lot.doc(did)
    if not doc.has_output:
        raise HTTPException(409, "This document was not modified.")
    dpi = max(60, min(300, dpi))

    def render() -> bytes:
        before = fitz.open(stream=lot.read(did, "before"), filetype="pdf")
        after = fitz.open(stream=lot.read(did, "after"), filetype="pdf")
        try:
            stored = (doc.last_outcome or {}).get("page_boxes") or {}
            boxes = stored.get(str(page)) or stored.get(page) or []
            return diff_image(before, after, page, boxes, dpi=dpi)
        finally:
            before.close()
            after.close()

    return Response(await _run(render), media_type="image/png")


@app.get("/api/lots/{sid}/documents/{did}/download")
async def download_one(request: Request, sid: str, did: str) -> FileResponse:
    lot = get_lot(sid)
    check_did(did)
    doc = lot.doc(did)
    if not doc.has_output:
        raise HTTPException(409, "This document was not modified.")
    _log(request, "download_one", sid=sid, names=[doc.filename], docs=1)
    return FileResponse(lot.edited(did), media_type="application/pdf",
                        filename=_output_name(doc.filename))


@app.get("/api/lots/{sid}/download")
async def download_all(request: Request, sid: str) -> Response:
    """Archive of the modified documents, each suffixed "_1"."""
    lot = get_lot(sid)
    modified = [d for d in lot.docs if d.has_output]
    if not modified:
        raise HTTPException(409, "No modified document to download.")

    if len(modified) == 1:
        d = modified[0]
        _log(request, "download", sid=sid, docs=1)
        return FileResponse(lot.edited(d.did), media_type="application/pdf",
                            filename=_output_name(d.filename))

    def build() -> bytes:
        buf = io.BytesIO()
        used: Dict[str, int] = {}
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for d in modified:
                name = _output_name(d.filename)
                # Two original files may bear the same name: we do not let one
                # silently overwrite the other.
                n = used.get(name, 0)
                used[name] = n + 1
                if n:
                    base, ext = os.path.splitext(name)
                    name = "%s (%d)%s" % (base, n + 1, ext)
                z.write(lot.edited(d.did), arcname=name)
        return buf.getvalue()

    data = await _run(build)
    _log(request, "download", sid=sid, docs=len(modified))
    stamp = time.strftime("%Y%m%d-%H%M")
    return Response(data, media_type="application/zip", headers={
        "Content-Disposition": 'attachment; filename="atelier-pdf-%s.zip"' % stamp,
    })


@app.post("/api/lots/{sid}/fonts")
async def upload_font(request: Request, sid: str, file: UploadFile = File(...),
                      target: str = Form("")) -> dict:
    """
    Manual upload of a source font, when the server does not have it.
    It applies to every document of the batch.
    """
    lot = get_lot(sid)
    raw = await file.read()
    if len(raw) > 12 * 1024 * 1024:
        raise HTTPException(413, "Font too large.")
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in (".ttf", ".otf", ".ttc"):
        raise HTTPException(400, "Accepted formats: .ttf, .otf, .ttc")

    path = os.path.join(lot.dir, "font_%s%s" % (hashlib.sha1(raw).hexdigest()[:12], ext))
    with open(path, "wb") as fh:
        fh.write(raw)

    names: List[str] = []
    try:
        from fontTools.ttLib import TTFont
        tt = TTFont(path, lazy=True, ignoreDecompileErrors=True)
        for nid in (6, 4, 1):
            n = tt["name"].getDebugName(nid) if "name" in tt else None
            if n and n not in names:
                names.append(n)
        tt.close()
    except Exception:
        pass

    for k in ([target] if target else []) + names:
        if k:
            lot.extra_fonts[k] = path
    lot.save()
    _log(request, "upload_font", sid=sid, names=[_safe_name(file.filename)], target=target, font_names=names, bytes=len(raw))
    return {"ok": True, "names": names}


# --------------------------------------------------------------------------
# Static frontend
# --------------------------------------------------------------------------

install_public_api(app, sys.modules[__name__])

if os.path.isdir(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
else:  # pragma: no cover
    @app.get("/")
    async def _placeholder() -> PlainTextResponse:
        return PlainTextResponse("frontend missing")
