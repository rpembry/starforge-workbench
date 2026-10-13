"""Explicit owner-only browser companion for the protected coordinator socket.

This app is never mounted by the normal Workbench factory. Its launcher binds
loopback only and creates one short-lived activation secret in process memory.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import ipaddress
import re
import secrets
import socket
import stat
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from coordinator.api import RequestBodyLimit

from .coordinator_ui import install

COOKIE = "coord_ui_session"
LOCAL_HOST = re.compile(r"coordinator-ui-[0-9a-f]{32}\.localhost\Z")


def _local_origin(value: str) -> bool:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        return False
    return (parsed.scheme == "http" and bool(LOCAL_HOST.fullmatch(parsed.hostname or ""))
            and port is not None and 1 <= port <= 65535 and not parsed.username
            and not parsed.password and not parsed.path.rstrip("/")
            and not parsed.query and not parsed.fragment)


def _workbench_link(value: str) -> bool:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        return False
    return (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            and port is not None and 1 <= port <= 65535 and not parsed.username
            and not parsed.password and not parsed.path.rstrip("/")
            and not parsed.query and not parsed.fragment)


class EphemeralSessions:
    def __init__(self, activation_secret: str, *, clock=time.monotonic,
                 activation_seconds: int = 300, session_seconds: int = 8 * 3600):
        if len(activation_secret) < 32:
            raise ValueError("activation secret must be at least 32 characters")
        self._digest = hashlib.sha256(activation_secret.encode()).digest()
        self._clock = clock
        self._activation_until = clock() + activation_seconds
        self._session_seconds = session_seconds
        self._sessions: dict[bytes, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def activate(self, supplied: str) -> tuple[str, str] | None:
        with self._lock:
            if (self._digest is None or self._clock() >= self._activation_until or
                    not hmac.compare_digest(self._digest, hashlib.sha256(supplied.encode()).digest())):
                return None
            self._digest = None  # A second activation cannot mint another session.
            session = secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(32)
            self._sessions[hashlib.sha256(session.encode()).digest()] = (
                csrf, self._clock() + self._session_seconds)
            return session, csrf

    def get(self, supplied: str | None) -> str | None:
        if not supplied:
            return None
        digest = hashlib.sha256(supplied.encode()).digest()
        with self._lock:
            row = self._sessions.get(digest)
            if not row:
                return None
            csrf, deadline = row
            if self._clock() >= deadline:
                del self._sessions[digest]
                return None
            return csrf

    def revoke(self, supplied: str | None) -> None:
        if supplied:
            with self._lock:
                self._sessions.pop(hashlib.sha256(supplied.encode()).digest(), None)


def create_local_app(coordinator_socket: str, *, activation_secret: str, origin: str,
                     workbench_url: str | None = None, clock=time.monotonic,
                     client_factory=None) -> FastAPI:
    if not _local_origin(origin):
        raise ValueError("coordinator UI origin must be exact loopback HTTP with an explicit port")
    if workbench_url is not None and not _workbench_link(workbench_url):
        raise ValueError("Workbench link must be an exact local HTTP origin")
    if not Path(coordinator_socket).is_absolute():
        raise ValueError("coordinator socket path must be absolute")
    sessions = EphemeralSessions(activation_secret, clock=clock)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.coordinator_local_boundary = True
    app.state.coordinator_origin = origin
    app.state.workbench_url = workbench_url

    @app.middleware("http")
    async def private_browser_boundary(request: Request, call_next):
        # Peer and Host checks are defense in depth. The independent one-use
        # activation and session are the authorization boundary against a proxy.
        if (str(request.base_url).rstrip("/") != origin or not request.client or
                request.client.host not in {"127.0.0.1", "::1", "testclient"}):
            return JSONResponse({"error": "local_only"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        # Chromium sends Origin: null for same-origin form POSTs under
        # no-referrer. same-origin retains exact Origin for CSRF checks while
        # sending no referrer to the optional other-loopback Workbench link.
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; "
            "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        return response

    async def operator(request: Request):
        csrf = sessions.get(request.cookies.get(COOKIE))
        if csrf is None:
            return _deny(401, "session_required")
        if request.method not in {"GET", "HEAD"}:
            if request.headers.get("origin") != origin:
                return _deny(403, "origin_rejected")
            form = await request.form()
            supplied = form.get("csrf_token")
            if not isinstance(supplied, str) or not hmac.compare_digest(csrf, supplied):
                return _deny(403, "csrf_rejected")
        request.state.csrf_token = csrf
        return True

    def _deny(status: int, code: str):
        from fastapi import HTTPException
        raise HTTPException(status_code=status, detail=code)

    @app.get("/")
    async def root():
        return RedirectResponse("/coordinator", status_code=303)

    @app.get("/activate", response_class=HTMLResponse)
    async def activation_page():
        return HTMLResponse('''<!doctype html><html lang="en"><meta charset="utf-8">
<title>Activate local coordinator · Starforge Workbench</title>
<script src="/assets/coordinator-activate.js" defer></script>
<body style="font:16px system-ui;max-width:42rem;margin:3rem auto;padding:1rem;background:#101722;color:#e5ebf3">
<h1>Activate local coordinator</h1><p id="activation-status" role="status">Checking local activation.</p>
<p>This one-use activation stays in this browser session and is not a Workbench bearer credential.</p>
</body></html>''')

    @app.get("/assets/coordinator-activate.js")
    async def activation_script():
        return FileResponse(Path(__file__).with_name("static") / "coordinator-activate.js",
                            media_type="application/javascript")

    @app.post("/activate")
    async def activate(request: Request):
        if request.headers.get("origin") != origin:
            return JSONResponse({"error": "origin_rejected"}, status_code=403)
        body = await request.body()
        if len(body) > 1024:
            return JSONResponse({"error": "invalid_activation"}, status_code=422)
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            payload = None
        if not isinstance(payload, dict) or set(payload) != {"secret"} or not isinstance(payload["secret"], str):
            return JSONResponse({"error": "invalid_activation"}, status_code=422)
        result = sessions.activate(payload["secret"])
        if result is None:
            return JSONResponse({"error": "activation_unavailable"}, status_code=403)
        session, _csrf = result
        response = JSONResponse({"activated": True})
        response.set_cookie(COOKIE, session, httponly=True, samesite="strict", path="/")
        return response

    @app.post("/logout", dependencies=[Depends(operator)])
    async def logout(request: Request):
        sessions.revoke(request.cookies.get(COOKIE))
        response = RedirectResponse("/activate", status_code=303)
        response.delete_cookie(COOKIE, path="/")
        return response

    install(app, operator, coordinator_socket, factory=client_factory)
    app.add_middleware(RequestBodyLimit, limit=131_072)
    return app


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Explicit local-only coordinator browser companion")
    parser.add_argument("--socket", required=True, help="owner-protected coordinator Unix socket")
    parser.add_argument("--port", required=True, type=int, help="unused loopback port")
    parser.add_argument("--workbench-origin", help="optional local Workbench dashboard origin")
    args = parser.parse_args(argv)
    host = f"coordinator-ui-{secrets.token_hex(16)}.localhost"
    origin = f"http://{host}:{args.port}"
    path = Path(args.socket)
    try:
        info = path.lstat()
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or
                info.st_mode & 0o077):
            raise ValueError("coordinator socket must be owner-owned with private permissions")
        addresses = socket.getaddrinfo(host, args.port, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_loopback for item in addresses):
            raise ValueError("coordinator UI hostname must resolve only to loopback")
        secret = secrets.token_urlsafe(32)
        app = create_local_app(str(path), activation_secret=secret, origin=origin,
                               workbench_url=args.workbench_origin)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Open {origin}/activate#{secret}", flush=True)
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port, proxy_headers=False, access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
