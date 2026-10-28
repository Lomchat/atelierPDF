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
import json
import os
import queue
import re
import shutil
import sys
import threading
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
                               Response, StreamingResponse)
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
UPLOADS_DIR = os.path.join(DATA_DIR, "uploads")

# Limits of the one-shot public API (/api/replace): its body is a single
# multipart request, so it stays bounded by what the proxies let through.
MAX_FILE_BYTES = 40 * 1024 * 1024
MAX_LOT_BYTES = 160 * 1024 * 1024
MAX_DOCS = 24

# Limits of the interactive batches, whose files arrive in chunks (see
# "Chunked uploads" below). The engine holds a whole document in memory — the
# bytes, MuPDF, pikepdf, the rewritten copy and the verification renders: a
# 270 MB PDF peaks at ~1.2 GB of RSS, so about 4.5x its size. Up to
# MAX_PARALLEL documents of a batch are processed at once, in each of the two
# workers: 1 GiB per file keeps the worst case of one batch near 18 GB.
LOT_MAX_FILE_BYTES = 1024 * 1024 * 1024
LOT_MAX_BYTES = 4 * 1024 * 1024 * 1024

# Chunked uploads. A chunk must stay < 100 MB: the Cloudflare Free proxy
# rejects any request body above that (413). The browser sends 32 MiB; this
# is the largest chunk the server accepts.
UPLOAD_CHUNK_BYTES = 32 * 1024 * 1024
UPLOAD_MIN_CHUNK_BYTES = 64 * 1024
# An upload nobody has touched for this long is abandoned and deleted.
UPLOAD_TTL = 3600
MAX_PENDING_UPLOADS = 256
# Space that must remain free on the data disk once an upload is reserved, on
# top of room for the rewritten copy of the file (as large as the original).
DISK_RESERVE_BYTES = 1024 * 1024 * 1024
MAX_PAGES = 800
SESSION_TTL = 3 * 3600
CLEANUP_INTERVAL = 600

# The documents of a batch are processed in parallel. The engine spends most of
# its time in pikepdf and MuPDF, which release the GIL: threads are enough.
MAX_PARALLEL = 4

for d in (DATA_DIR, LOTS_DIR, FONTS_DIR, UPLOADS_DIR):
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

    def path(self, did: str, version: str) -> str:
        d = self.doc(did)
        return self.edited(did) if (version == "after" and d.has_output) \
            else self.original(did)

    def read(self, did: str, version: str) -> bytes:
        return self.read_path(self.path(did, version))

    @staticmethod
    def read_path(path: str) -> bytes:
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
            _purge_uploads(now)
        except Exception:
            pass
        await asyncio.sleep(CLEANUP_INTERVAL)


def _purge_uploads(now: float) -> None:
    """Deletes the uploads left idle for UPLOAD_TTL: abandoned, or finished."""
    for name in os.listdir(UPLOADS_DIR):
        p = os.path.join(UPLOADS_DIR, name)
        try:
            if os.path.isdir(p) and now - os.path.getmtime(p) > UPLOAD_TTL:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass


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


def _check_room(lot: Lot, name: str, size: int) -> None:
    """Batch limits, checked before a file is accepted into `lot`."""
    if len(lot.docs) >= MAX_DOCS:
        raise HTTPException(413, "Maximum %d documents per batch." % MAX_DOCS)
    if size > LOT_MAX_FILE_BYTES:
        raise HTTPException(413, '"%s" is over %s.' % (name, _fmt_bytes(LOT_MAX_FILE_BYTES)))
    if lot.total_bytes + size > LOT_MAX_BYTES:
        raise HTTPException(413, "The batch is over %s in total." % _fmt_bytes(LOT_MAX_BYTES))


def _fmt_bytes(n: float) -> str:
    return ("%.1f GB" % (n / 1024 ** 3)) if n >= 1024 ** 3 else ("%d MB" % (n // 1024 ** 2))


async def _ingest_path(lot: Lot, name: str, path: str, size: int, locale: str) -> Doc:
    """
    Moves a file already on disk into the batch and analyses it. `path` must
    sit on the data disk: the move is a rename, never a copy of gigabytes.
    """
    _check_room(lot, name, size)
    with open(path, "rb") as fh:
        if fh.read(5) != b"%PDF-":
            raise HTTPException(400, '"%s" is not a PDF.' % name)

    did = "d%d" % lot.seq
    lot.seq += 1
    os.makedirs(lot.doc_dir(did), exist_ok=True)
    os.replace(path, lot.original(did))

    doc = Doc(did=did, filename=name, size=size)
    try:
        raw = await asyncio.get_event_loop().run_in_executor(
            None, lot.read_path, lot.original(did))
        analysis = await _run(analyze, raw, [], None, locale,
                              cache_key=lot.cache_key(did))
        del raw
        if analysis.pages > MAX_PAGES:
            raise ValueError("document too long (%d pages, maximum %d)"
                             % (analysis.pages, MAX_PAGES))
        doc.analysis = analysis.to_dict()
        doc.pages = analysis.pages
    except Exception as e:
        doc.error = str(e)
    lot.docs.append(doc)
    return doc


async def _ingest(lot: Lot, files: List[UploadFile], locale: str) -> List[Doc]:
    """Stores new multipart files and analyses them structurally."""
    added: List[Doc] = []
    try:
        for up in files:
            name = _safe_name(up.filename)
            if len(lot.docs) >= MAX_DOCS:
                raise HTTPException(413, "Maximum %d documents per batch." % MAX_DOCS)
            # Streamed to disk, never held whole in memory.
            tmp = os.path.join(lot.dir, "incoming-%s.tmp" % uuid.uuid4().hex[:8])
            size = 0
            try:
                with open(tmp, "wb") as fh:
                    while True:
                        piece = await up.read(1024 * 1024)
                        if not piece:
                            break
                        size += len(piece)
                        _check_room(lot, name, size)
                        fh.write(piece)
                if size:
                    added.append(await _ingest_path(lot, name, tmp, size, locale))
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
    finally:
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


# --------------------------------------------------------------------------
# Chunked uploads
# --------------------------------------------------------------------------
#
# The public proxy (Cloudflare Free) refuses any request body over 100 MB, and
# a flaky line must not restart a gigabyte from zero. A file therefore travels
# as numbered chunks:
#
#   POST   /api/uploads                     {filename, size, chunk_size, lot?}
#   PUT    /api/uploads/{uid}/chunks/{i}?offset=   raw bytes of chunk i
#   GET    /api/uploads/{uid}               which chunks arrived (resume)
#   DELETE /api/uploads/{uid}               abandon
#   POST   /api/lots/from-uploads           {uploads: [uid], locale}
#   POST   /api/lots/{sid}/documents/from-uploads
#
# The full size is reserved on disk up front (fallocate): a disk too full is
# refused before the first byte, and two concurrent uploads cannot both count
# on the same free space. Each chunk is written at its offset, straight to the
# file, then a marker file records it — one file per chunk, so the two uvicorn
# workers never race on a shared state file. Sending a chunk again rewrites the
# same bytes at the same place: idempotent. Finishing is a rename into the
# batch, never a copy, and leaves a tombstone so a retried finish (response
# lost on the way) returns the same result instead of failing.

_UID_RE = re.compile(r"^[0-9a-f]{32}$")


class UploadIn(BaseModel):
    filename: str = Field("document.pdf", max_length=1024)
    size: int
    chunk_size: int = UPLOAD_CHUNK_BYTES
    lot: Optional[str] = None


class FromUploads(BaseModel):
    uploads: List[str] = Field(..., min_length=1, max_length=MAX_DOCS)
    locale: str = "fr"


def _upload_dir(uid: str) -> str:
    if not _UID_RE.match(uid or ""):
        raise HTTPException(400, "Invalid upload id.")
    return os.path.join(UPLOADS_DIR, uid)


def _upload_meta(uid: str) -> dict:
    try:
        with open(os.path.join(_upload_dir(uid), "meta.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        raise HTTPException(404, "Unknown or expired upload. Send the file again.")


def _upload_done(uid: str) -> Optional[dict]:
    try:
        with open(os.path.join(_upload_dir(uid), "done.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _received(uid: str) -> List[int]:
    try:
        return sorted(int(n) for n in os.listdir(os.path.join(_upload_dir(uid), "chunks")))
    except OSError:
        return []


def _upload_state(meta: dict) -> dict:
    got = _received(meta["id"])
    return {"id": meta["id"], "filename": meta["filename"], "size": meta["size"],
            "chunk_size": meta["chunk_size"], "chunks": meta["chunks"],
            "received": got, "complete": len(got) == meta["chunks"],
            "done": _upload_done(meta["id"])}


@app.get("/api/limits")
async def limits() -> dict:
    return {"max_file_bytes": LOT_MAX_FILE_BYTES, "max_lot_bytes": LOT_MAX_BYTES,
            "max_docs": MAX_DOCS, "chunk_bytes": UPLOAD_CHUNK_BYTES,
            "free_bytes": max(0, shutil.disk_usage(UPLOADS_DIR).free - DISK_RESERVE_BYTES)}


@app.post("/api/uploads")
async def start_upload(request: Request, body: UploadIn) -> dict:
    name = _safe_name(body.filename)
    _log(request, "upload_start", names=[name], bytes=body.size, sid=body.lot)
    if body.size <= 0:
        raise HTTPException(400, '"%s" is empty.' % name)
    # A small file may come as one chunk of exactly its size.
    if not min(UPLOAD_MIN_CHUNK_BYTES, body.size) <= body.chunk_size <= UPLOAD_CHUNK_BYTES:
        raise HTTPException(400, "chunk_size must be between %d and %d bytes."
                            % (UPLOAD_MIN_CHUNK_BYTES, UPLOAD_CHUNK_BYTES))
    if body.lot:
        _check_room(get_lot(body.lot), name, body.size)
    elif body.size > LOT_MAX_FILE_BYTES:
        raise HTTPException(413, '"%s" is over %s.' % (name, _fmt_bytes(LOT_MAX_FILE_BYTES)))
    if len(os.listdir(UPLOADS_DIR)) >= MAX_PENDING_UPLOADS:
        raise HTTPException(429, "Too many uploads in progress. Try again later.")

    # The file is reserved below, and its rewritten copy will need as much.
    need = 2 * body.size + DISK_RESERVE_BYTES
    free = shutil.disk_usage(UPLOADS_DIR).free
    if free < need:
        raise HTTPException(507, "Not enough free space on the server for \"%s\" "
                            "(%s needed, %s free)." % (name, _fmt_bytes(need), _fmt_bytes(free)))

    uid = uuid.uuid4().hex
    folder = os.path.join(UPLOADS_DIR, uid)
    os.makedirs(os.path.join(folder, "chunks"))
    meta = {"id": uid, "filename": name, "size": body.size,
            "chunk_size": body.chunk_size,
            "chunks": -(-body.size // body.chunk_size),
            "lot": body.lot, "created": time.time()}
    try:
        fd = os.open(os.path.join(folder, "data.part"), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            os.posix_fallocate(fd, 0, body.size)
        finally:
            os.close(fd)
        with open(os.path.join(folder, "meta.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh)
    except OSError:
        shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(507, 'Not enough free space on the server for "%s".' % name)
    _log(request, "upload_start", upload=uid, chunks=meta["chunks"])
    return _upload_state(meta)


@app.put("/api/uploads/{uid}/chunks/{index}")
async def put_chunk(request: Request, uid: str, index: int, offset: int) -> dict:
    meta = _upload_meta(uid)
    folder = _upload_dir(uid)
    # A successful chunk is not worth an audit record (a gigabyte is 32 of
    # them); a refused one is.
    request.state.audit.update(event="upload_chunk", upload=uid, chunk=index, _quiet=True)
    if _upload_done(uid) is not None:
        raise HTTPException(409, "This upload is already finished.")
    if not 0 <= index < meta["chunks"]:
        raise HTTPException(400, "Chunk %d out of range (0-%d)." % (index, meta["chunks"] - 1))
    if offset != index * meta["chunk_size"]:
        raise HTTPException(409, "Chunk %d starts at offset %d, not %d."
                            % (index, index * meta["chunk_size"], offset))
    expected = min(meta["chunk_size"], meta["size"] - offset)
    declared = request.headers.get("content-length")
    if declared is not None and declared != str(expected):
        raise HTTPException(400, "Chunk %d must be %d bytes, not %s."
                            % (index, expected, declared))

    path = os.path.join(folder, "data.part")
    try:
        fd = os.open(path, os.O_WRONLY)
    except OSError:
        raise HTTPException(409, "This upload is being finished.")
    loop = asyncio.get_event_loop()
    got, head, buf = 0, b"", bytearray()

    def flush(data: bytes, at: int) -> None:
        view = memoryview(data)
        while view:
            n = os.pwrite(fd, view, at)
            view, at = view[n:], at + n

    try:
        async for piece in request.stream():
            if got + len(buf) + len(piece) > expected:
                raise HTTPException(400, "Chunk %d is longer than %d bytes." % (index, expected))
            if len(head) < 5:
                head += piece[:5 - len(head)]
            buf += piece
            # Written as it arrives, a few MiB at a time, off the event loop.
            if len(buf) >= 4 * 1024 * 1024:
                await loop.run_in_executor(None, flush, bytes(buf), offset + got)
                got += len(buf)
                buf.clear()
        if buf:
            await loop.run_in_executor(None, flush, bytes(buf), offset + got)
            got += len(buf)
    finally:
        os.close(fd)
    if got != expected:
        raise HTTPException(400, "Chunk %d is incomplete: %d of %d bytes." % (index, got, expected))
    if index == 0 and head != b"%PDF-":
        shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(400, '"%s" is not a PDF.' % meta["filename"])

    open(os.path.join(folder, "chunks", str(index)), "w").close()
    os.utime(folder)          # still alive: pushes back the expiry
    got_all = _received(uid)
    return {"id": uid, "index": index, "received": len(got_all),
            "chunks": meta["chunks"], "complete": len(got_all) == meta["chunks"]}


@app.get("/api/uploads/{uid}")
async def upload_state(uid: str) -> dict:
    return _upload_state(_upload_meta(uid))


@app.delete("/api/uploads/{uid}")
async def cancel_upload(request: Request, uid: str) -> dict:
    folder = _upload_dir(uid)
    _log(request, "upload_cancel", upload=uid)
    shutil.rmtree(folder, ignore_errors=True)
    return {"ok": True}


async def _consume(lot: Lot, uids: List[str], locale: str) -> List[Doc]:
    """Turns finished uploads into documents of `lot`. Safe to call again."""
    added: List[Doc] = []
    try:
        for uid in uids:
            folder = _upload_dir(uid)
            done = _upload_done(uid)
            if done is not None:
                if done.get("lot") != lot.sid:
                    raise HTTPException(409, "This upload already belongs to another batch.")
                added.extend(d for d in lot.docs if d.did == done.get("doc"))
                continue
            meta = _upload_meta(uid)
            if meta.get("lot") and meta["lot"] != lot.sid:
                raise HTTPException(409, "This upload was started for another batch.")
            missing = sorted(set(range(meta["chunks"])) - set(_received(uid)))
            if missing:
                raise HTTPException(409, '"%s" is incomplete: %d chunk(s) missing (first: %d).'
                                    % (meta["filename"], len(missing), missing[0]))
            # Two finishes of the same upload at once (two workers): one wins.
            try:
                os.close(os.open(os.path.join(folder, "finishing"),
                                 os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            except FileExistsError:
                raise HTTPException(409, "This upload is already being finished.")
            try:
                doc = await _ingest_path(lot, meta["filename"],
                                         os.path.join(folder, "data.part"),
                                         meta["size"], locale)
            except BaseException:
                try:
                    os.remove(os.path.join(folder, "finishing"))
                except OSError:
                    pass
                raise
            lot.save()
            with open(os.path.join(folder, "done.json"), "w", encoding="utf-8") as fh:
                json.dump({"lot": lot.sid, "doc": doc.did}, fh)
            os.utime(folder)
            added.append(doc)
    finally:
        lot.save()
    return added


def _audit_files(docs: List[Doc]) -> List[dict]:
    return [{"filename": d.filename, "size": d.size, "pages": d.pages, "error": d.error}
            for d in docs]


@app.post("/api/lots/from-uploads")
async def create_lot_from_uploads(request: Request, body: FromUploads) -> JSONResponse:
    _log(request, "upload", uploads=body.uploads)
    for uid in body.uploads:
        _upload_dir(uid)
    # A retried request (its first answer lost) finds the batch it created.
    lot = None
    for uid in body.uploads:
        done = _upload_done(uid)
        if done is not None:
            lot = get_lot(done.get("lot", ""))
            break
    created = lot is None
    if created:
        lot = Lot(sid=uuid.uuid4().hex[:20], created=time.time())
        os.makedirs(lot.dir, exist_ok=True)
    try:
        await _consume(lot, body.uploads, body.locale)
    except HTTPException:
        if created and not lot.docs:
            shutil.rmtree(lot.dir, ignore_errors=True)
        raise
    if not lot.docs:
        shutil.rmtree(lot.dir, ignore_errors=True)
        raise HTTPException(400, "No usable document.")
    LOTS[lot.sid] = lot
    _log(request, "upload", sid=lot.sid, docs=len(lot.docs), bytes=lot.total_bytes,
         names=[d.filename for d in lot.docs], files=_audit_files(lot.docs))
    return JSONResponse(lot.to_dict())


@app.post("/api/lots/{sid}/documents/from-uploads")
async def add_documents_from_uploads(request: Request, sid: str,
                                     body: FromUploads) -> JSONResponse:
    lot = get_lot(sid)
    _log(request, "add", sid=sid, uploads=body.uploads)
    added = await _consume(lot, body.uploads, body.locale)
    _log(request, "add", sid=sid, docs=len(added), names=[d.filename for d in added],
         files=_audit_files(added))
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
        # Opened from the file: MuPDF reads only what the page needs, instead
        # of the whole document (up to a gigabyte) for every page.
        d = fitz.open(lot.path(did, version), filetype="pdf")
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
        before = fitz.open(lot.path(did, "before"), filetype="pdf")
        after = fitz.open(lot.path(did, "after"), filetype="pdf")
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

    # A batch can weigh gigabytes: the archive is streamed as it is written,
    # never assembled in memory nor on the (small) disk. zipfile supports an
    # unseekable output by writing data descriptors after each member.
    pipe: "queue.Queue[Optional[bytes]]" = queue.Queue(maxsize=8)
    stop = threading.Event()
    failed: List[BaseException] = []

    def put(item: Optional[bytes]) -> None:
        while not stop.is_set():
            try:
                pipe.put(item, timeout=1)
                return
            except queue.Full:
                pass
        raise OSError("download abandoned")

    class _Sink:
        def write(self, b) -> int:
            put(bytes(b))
            return len(b)

        def flush(self) -> None:
            pass

    def build() -> None:
        used: Dict[str, int] = {}
        try:
            with zipfile.ZipFile(_Sink(), "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
                for d in modified:
                    name = _output_name(d.filename)
                    # Two original files may bear the same name: we do not let
                    # one silently overwrite the other.
                    n = used.get(name, 0)
                    used[name] = n + 1
                    if n:
                        base, ext = os.path.splitext(name)
                        name = "%s (%d)%s" % (base, n + 1, ext)
                    z.write(lot.edited(d.did), arcname=name)
        except BaseException as e:
            failed.append(e)
        finally:
            try:
                put(None)
            except OSError:
                pass

    def get() -> Optional[bytes]:
        while not stop.is_set():
            try:
                return pipe.get(timeout=1)
            except queue.Empty:
                pass
        return None

    async def stream():
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, build)
        try:
            while True:
                piece = await loop.run_in_executor(None, get)
                if piece is None:
                    break
                yield piece
            if failed:
                # Cut the connection rather than end a truncated archive cleanly.
                raise RuntimeError("archive failed: %s" % failed[0])
        finally:
            stop.set()

    _log(request, "download", sid=sid, docs=len(modified))
    stamp = time.strftime("%Y%m%d-%H%M")
    return StreamingResponse(stream(), media_type="application/zip", headers={
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
