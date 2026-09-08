"""Persistent operation metadata. Never stores request/response binary bodies."""
import asyncio
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse


class AuditStore:
    def __init__(self, path, legacy_path=None):
        self.path = str(path)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS operations (id INTEGER PRIMARY KEY, t REAL NOT NULL, source TEXT NOT NULL, status INTEGER NOT NULL, event TEXT NOT NULL, record TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS operations_filters ON operations(source, status, id)")
            db.execute("CREATE TABLE IF NOT EXISTS migrations (name TEXT PRIMARY KEY)")
            if legacy_path and not db.execute("SELECT 1 FROM migrations WHERE name='usage.log'").fetchone():
                if os.path.isfile(legacy_path):
                    with open(legacy_path, encoding="utf-8") as fh:
                        for line in fh:
                            try:
                                rec = json.loads(line)
                                rec.update(source="historique", status=200)
                                self._insert(db, rec)
                            except (ValueError, KeyError, TypeError):
                                continue
                db.execute("INSERT INTO migrations VALUES ('usage.log')")
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _insert(db, rec):
        db.execute("INSERT INTO operations(t, source, status, event, record) VALUES (?, ?, ?, ?, ?)",
                   (rec["t"], rec["source"], rec["status"], rec.get("event", "request"), json.dumps(rec, ensure_ascii=False)))

    def write(self, rec):
        with self.connect() as db:
            self._insert(db, rec)

    def query(self, page, limit, source, status, q):
        where, params = [], []
        if source:
            where.append("source = ?")
            params.append(source)
        if status == "error":
            where.append("status >= 400")
        elif status == "success":
            where.append("status < 400")
        if q:
            where.append("record LIKE ? ESCAPE '\\'")
            params.append("%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        clause = " WHERE " + " AND ".join(where) if where else ""
        with self.connect() as db:
            total = db.execute("SELECT COUNT(*) FROM operations" + clause, params).fetchone()[0]
            rows = db.execute("SELECT id, record FROM operations" + clause + " ORDER BY id DESC LIMIT ? OFFSET ?", params + [limit, (page - 1) * limit]).fetchall()
        return {"total": total, "page": page, "limit": limit, "items": [{**json.loads(rec), "id": ident} for ident, rec in rows]}


ADMIN_COOKIE = "__Host-pdf_admin"
ADMIN_SESSION_TTL = 8 * 3600
ADMIN_HEADERS = {"Cache-Control": "no-store", "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff"}


def install_admin(app, store):
    username = os.environ.get("ADMIN_USERNAME", "")
    password = os.environ.get("ADMIN_PASSWORD", "")
    if not username or not password:
        raise RuntimeError("Set ADMIN_USERNAME and ADMIN_PASSWORD in the root .env")
    with store.connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS admin_sessions (token_hash TEXT PRIMARY KEY, expires REAL NOT NULL)")
    templates = Path(__file__).parent / "templates"

    def token_hash(token):
        # Changing either credential invalidates existing sessions. Only a
        # digest is stored; the random bearer token stays in the HttpOnly cookie.
        return hmac.new(password.encode(), (username + "\0" + token).encode(), hashlib.sha256).hexdigest()

    def signed_in(request):
        token = request.cookies.get(ADMIN_COOKIE, "")
        if not token or len(token) > 128:
            return False
        with store.connect() as db:
            return db.execute("SELECT 1 FROM admin_sessions WHERE token_hash=? AND expires>?",
                              (token_hash(token), time.time())).fetchone() is not None

    def authenticate(request: Request):
        if not signed_in(request):
            # No WWW-Authenticate header: the browser must never open a prompt.
            raise HTTPException(401, "Session expirée. Veuillez vous reconnecter.", headers=ADMIN_HEADERS)

    def check_origin(request):
        origin = request.headers.get("origin")
        expected = str(request.base_url).rstrip("/")
        if (origin and origin != expected) or request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403, "Origine de la requête refusée.", headers=ADMIN_HEADERS)

    def login_form(error=False):
        html = (templates / "login.html").read_text(encoding="utf-8")
        message = '<p class="login-error" role="alert">Utilisateur ou mot de passe incorrect.</p>' if error else ""
        return HTMLResponse(html.replace("<!-- LOGIN_ERROR -->", message),
                            status_code=401 if error else 200, headers=ADMIN_HEADERS)

    @app.get("/admin/login", include_in_schema=False)
    def login_page(request: Request):
        if signed_in(request):
            return RedirectResponse("/admin", status_code=303, headers=ADMIN_HEADERS)
        return login_form()

    @app.post("/admin/login", include_in_schema=False)
    def login(request: Request, user: str = Form("", max_length=256),
              secret: str = Form("", max_length=1024)):
        check_origin(request)
        valid_user = secrets.compare_digest(user.encode(), username.encode())
        valid_password = secrets.compare_digest(secret.encode(), password.encode())
        if not (valid_user & valid_password):
            return login_form(error=True)
        token = secrets.token_urlsafe(32)
        with store.connect() as db:
            db.execute("DELETE FROM admin_sessions WHERE expires<=? OR token_hash=?",
                       (time.time(), token_hash(request.cookies.get(ADMIN_COOKIE, ""))))
            db.execute("INSERT INTO admin_sessions VALUES (?, ?)", (token_hash(token), time.time() + ADMIN_SESSION_TTL))
        response = RedirectResponse("/admin", status_code=303, headers=ADMIN_HEADERS)
        response.set_cookie(ADMIN_COOKIE, token, max_age=ADMIN_SESSION_TTL, secure=True,
                            httponly=True, samesite="strict", path="/")
        return response

    @app.post("/admin/logout", include_in_schema=False)
    def logout(request: Request):
        check_origin(request)
        with store.connect() as db:
            db.execute("DELETE FROM admin_sessions WHERE token_hash=?", (token_hash(request.cookies.get(ADMIN_COOKIE, "")),))
        response = RedirectResponse("/admin/login", status_code=303, headers=ADMIN_HEADERS)
        response.delete_cookie(ADMIN_COOKIE, secure=True, httponly=True, samesite="strict", path="/")
        return response

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/", include_in_schema=False)
    def admin_page(request: Request):
        if not signed_in(request):
            return RedirectResponse("/admin/login", status_code=303, headers=ADMIN_HEADERS)
        return FileResponse(templates / "admin.html", headers=ADMIN_HEADERS)

    @app.get("/api/admin/operations", include_in_schema=False, dependencies=[Depends(authenticate)])
    def operations(page: int = Query(1, ge=1), limit: int = Query(50, ge=1, le=100),
                   source: str = Query("", pattern="^(|api|plateforme|historique)$"),
                   status: str = Query("", pattern="^(|success|error)$"), q: str = Query("", max_length=200)):
        return JSONResponse(store.query(page, limit, source, status, q), headers=ADMIN_HEADERS)


def install_audit(app, store):
    @app.middleware("http")
    async def audit_request(request: Request, call_next):
        path = request.url.path
        if not (path.startswith("/api/lots") or path == "/api/replace"):
            return await call_next(request)
        started = time.monotonic()
        request.state.audit = {}
        request.state.operation_id = uuid.uuid4().hex
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Operation-Id"] = request.state.operation_id
            if not path.endswith(".png"):
                response.headers["Cache-Control"] = "no-store"
            return response
        except Exception as exc:
            request.state.audit["error"] = type(exc).__name__
            raise
        finally:
            referrer = urlsplit(request.headers.get("referer", ""))
            source = "plateforme" if (request.headers.get("X-Atelier-Source") == "plateforme" or
                (referrer.netloc == request.url.netloc and referrer.path == "/")) else "api"
            rec = {"t": time.time(), "operation_id": request.state.operation_id, "source": source,
                   "ip": request.client.host if request.client else "", "method": request.method,
                   "path": path, **request.path_params, "query": request.url.query[:2048], "status": status,
                   "duration_ms": round((time.monotonic() - started) * 1000),
                   "event": getattr(request.scope.get("endpoint"), "__name__", "request"),
                   **request.state.audit}
            # SQLite transactions serialize writes across the uvicorn workers.
            await asyncio.to_thread(store.write, rec)
