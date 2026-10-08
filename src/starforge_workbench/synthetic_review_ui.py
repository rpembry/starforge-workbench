"""Unmounted offline browser review fixture, never production human authentication.

No launcher, main Workbench mount, MCP registration or live identity configuration
is provided. The explicit synthetic factory accepts only a fake local endpoint.
Same-user filesystem/browser access is outside this fixture's security claim.
"""
import html
import json
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .diagnostic_batch import DiagnosticWorker
from .diagnostic_release import FakeReleaseEndpoint, ReleaseBroker, automatic_status
from .diagnostic_vm_bridge import SupervisorDiagnosticFence
from .synthetic_review_authority import SyntheticHumanAuthority

COOKIE = 'synthetic_review_session'
MAX_BODY = 2048
MAX_VIEWS = 64
VIEW_SECONDS = 120


def _valid_origin(origin):
    try:
        value = urlsplit(origin)
        return (value.scheme == 'http' and value.port is not None
                and 1 <= value.port <= 65535
                and re.fullmatch(r'review-ui-[a-f0-9]{32}\.localhost', value.hostname or '')
                and not value.username and not value.password
                and not value.path and not value.query and not value.fragment)
    except (TypeError, ValueError):
        return False


def create_synthetic_review_app(*, broker=None, authority=None, worker=None, fence=None,
                                origin=None, enabled=False, clock=time.time):
    """Disabled unless explicitly composed with synthetic owner fixtures only."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
    if enabled is not True:
        return app
    if (type(broker) is not ReleaseBroker or type(authority) is not SyntheticHumanAuthority
            or type(worker) is not DiagnosticWorker or type(fence) is not SupervisorDiagnosticFence
            or type(broker.endpoint) is not FakeReleaseEndpoint or broker.authority is not authority
            or not {'failed', 'report_ready'} <= broker.policy.automatic_statuses
            or broker.ownership is not fence.ownership or worker.ownership is not fence.ownership
            or broker.path != worker.path or broker.job_id != worker.job_id
            or broker.attempt_id != worker.attempt_id
            or broker.job_id != fence.binding['job_id'] or broker.attempt_id != fence.binding['attempt_id']
            or any(authority.path == path or authority.path.is_relative_to(path)
                   or path.is_relative_to(authority.path) for path in (broker.path, broker.endpoint.path))
            or not _valid_origin(origin)):
        raise ValueError('Invalid offline synthetic review composition')
    views = {}
    lock = threading.Lock()

    def denied(code=403):
        return JSONResponse(automatic_status('failed'), status_code=code)

    from starlette.exceptions import HTTPException

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return denied(exc.status_code)

    @app.middleware('http')
    async def boundary(request, call_next):
        forwarded = any(name in request.headers for name in
                        ('forwarded', 'x-forwarded-for', 'x-forwarded-host', 'cf-connecting-ip'))
        if (str(request.base_url).rstrip('/') != origin or request.url.query or not request.client
                or request.client.host not in {'127.0.0.1', '::1', 'testclient'} or forwarded
                or any(name in request.headers for name in
                       ('authorization', 'cf-access-jwt-assertion', 'cf-access-client-id', 'cf-access-client-secret'))):
            response = denied()
        else:
            try:
                session = authority.resolve(request.cookies.get(COOKIE))
                request.state.review_session = session
                if request.method not in {'GET', 'HEAD'}:
                    if request.headers.get('origin') != origin:
                        return secured(denied())
                    if request.headers.get('content-type', '').split(';', 1)[0] != 'application/x-www-form-urlencoded':
                        return secured(denied(422))
                    size = 0
                    chunks = []
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > MAX_BODY:
                            return secured(denied(413))
                        chunks.append(chunk)
                    from urllib.parse import parse_qsl
                    fields = parse_qsl(b''.join(chunks).decode('ascii'), keep_blank_values=True,
                                       strict_parsing=True, max_num_fields=4)
                    if (len(fields) != 3 or len({key for key, _ in fields}) != 3
                            or {key for key, _ in fields} != {'csrf_token', 'snapshot', 'decision'}):
                        return secured(denied(422))
                    form = dict(fields)
                    import hmac
                    if not hmac.compare_digest(form['csrf_token'], session.csrf):
                        return secured(denied())
                    request.state.review_form = form
                response = await call_next(request)
            except Exception:
                # Untrusted identity, source, path and broker failures are content-free.
                response = denied()
        return secured(response)

    def secured(response):
        response.headers['Cache-Control'] = 'no-store, max-age=0'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = (
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
            "base-uri 'none'; frame-ancestors 'none'")
        return response

    def current_session(request):
        # Revalidate after body parsing/lock waits, including revocation, which
        # does not otherwise call authenticate. Never refresh the fixture expiry.
        current = authority.resolve(request.cookies.get(COOKIE))
        if current.credential is not request.state.review_session.credential:
            raise ValueError('Synthetic session unavailable')
        return current

    @app.get('/review', response_class=HTMLResponse)
    def review(request: Request):
        session = request.state.review_session
        with fence.scope():
            session = current_session(request)
            detail = worker.local_report()
            snapshot = broker.review()
        with lock:
            expired = [key for key, value in views.items() if value[2] <= clock()]
            for key in expired:
                del views[key]
            if len(views) >= MAX_VIEWS:
                return denied(429)
            handle = secrets.token_urlsafe(24)
            views[handle] = (session.credential, snapshot, clock()+VIEW_SECONDS)
        # Escaped string representation plus exact hex makes CR/LF, NUL, invalid
        # UTF-8 and bidi/control bytes visible without browser normalization.
        readable = json.dumps(snapshot.payload.decode('utf-8', errors='backslashreplace'), ensure_ascii=True)
        byte_view = snapshot.payload.hex(' ')
        escape = html.escape
        forms = ''.join(
            '<form method="post" action="/review/decision">'
            f'<input type="hidden" name="snapshot" value="{escape(handle)}">'
            f'<input type="hidden" name="csrf_token" value="{escape(session.csrf)}">'
            f'<input type="hidden" name="decision" value="{decision}">'
            f'<button type="submit">{label}</button></form>'
            for decision, label in (('approve', 'Simulate approval'), ('reject', 'Reject'),
                                    ('defer', 'Defer'), ('revoke', 'Revoke current approval')))
        return HTMLResponse(
            '<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>Offline synthetic release review · Workbench</title>'
            '<style>pre{white-space:pre-wrap;overflow-wrap:anywhere;max-width:100%;overflow-x:auto;'
            'border:1px solid #536073;padding:.75rem}form{display:inline-block;margin:.4rem .4rem .4rem 0}'
            'button{min-height:44px;padding:.65rem 1rem;font:inherit}details{margin-top:1rem}</style>'
            '<body style="font:16px system-ui;max-width:60rem;margin:2rem auto;padding:1rem;'
            'background:#101722;color:#e5ebf3"><h1>Offline synthetic release review</h1>'
            '<p>No actual human verification or outbound network delivery. This unmounted '
            'fixture does not exclude an agent with the same OS user or browser access.</p>'
            f'<h2>Exact outbound destination</h2><pre>{escape(snapshot.destination)}</pre>'
            f'<p>Revision {snapshot.revision} · {len(snapshot.payload)} bytes</p>'
            f'<p>Job {escape(snapshot.job_id)} · Attempt {escape(snapshot.attempt_id)} · '
            f'Operation {escape(snapshot.operation_id)}</p>'
            f'<h2>Escaped byte text</h2><pre>{escape(readable)}</pre>'
            f'<h2>Exact hexadecimal bytes</h2><pre>{escape(byte_view)}</pre>'
            '<p>Approval binds this immutable byte sequence, destination, revision, '
            'attempt and expiry. Editing requires a new preview.</p>'
            f'{forms}<details><summary>Local diagnostic detail — never outbound approval</summary>'
            f'<pre>{escape(detail.decode("utf-8"))}</pre></details></body></html>')

    @app.post('/review/decision')
    def decide(request: Request):
        form = request.state.review_form
        session = request.state.review_session
        if form['decision'] not in {'approve', 'reject', 'defer', 'revoke'}:
            return denied(422)
        with lock:
            view = views.pop(form['snapshot'], None)
        if view is None or view[0] is not session.credential or clock() >= view[2]:
            return denied(409)
        with fence.scope():
            session = current_session(request)
            worker.local_report()
            if broker.review() != view[1]:
                return denied(409)
            if form['decision'] == 'revoke':
                broker.revoke()
            else:
                broker.decide(view[1], session.credential, decision=form['decision'], ttl=300)
        # No dispatch endpoint: only the explicit fake local driver can dispatch.
        return RedirectResponse('/review', status_code=303)

    @app.get('/status')
    def status(request: Request):
        with fence.scope():
            current_session(request)
            worker.local_report()  # Missing, changed or non-ready evidence cannot claim readiness.
            return broker.status('report_ready')

    return app
