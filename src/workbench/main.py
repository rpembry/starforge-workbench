"""FastAPI factory. Start explicitly on 127.0.0.1; no module-import side effects."""
import os
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Form, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBearer, APIKeyHeader
from starlette.exceptions import HTTPException
from pydantic import BaseModel, Field, ValidationError

from .auth import Auth, CloudflareAuth
from .models import (ActionIn, ActionPatch, ArtifactIn, EventIn, ObjectiveIn,
                     RunIn, RunLink, RunPatch, Transition, CollectorIn, ImportBatch,
                     ProviderAttentionIn, ProviderGenerationIn, RegisteredSessionIn,
                     InstructionClaimIn, InstructionIn, InstructionLeaseIn,
                     InstructionResultIn, InstructionPreviewIn,
                     WorkItemProjectionIn, WorkItemActionLinkIn)
from .repository import Problem, SQLiteRepository
from .response_preview import ResponsePreviewHub
from .settings import load_settings


class BrowserWorkspaceEntryIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    url: str
    match: Literal['origin', 'url'] = 'origin'


class BrowserWorkspacePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    url: str | None = None
    match: Literal['origin', 'url'] | None = None


def create_app(repository=None, auth=None, settings=None, instruction_claims_enabled=None):
    if settings is None:
        settings = load_settings()
    if instruction_claims_enabled is None:
        configured = os.environ.get('WB_INSTRUCTION_CLAIMS_ENABLED', '0')
        if configured not in {'0', '1'}:
            raise RuntimeError('WB_INSTRUCTION_CLAIMS_ENABLED must be 0 or 1')
        instruction_claims_enabled = configured == '1'
    if auth is None:
        mode = os.environ.get('WB_AUTH_MODE', 'local')
        if mode == 'cloudflare':
            path = os.environ.get('WB_ACCESS_CONFIG')
            if not path:
                raise RuntimeError('WB_ACCESS_CONFIG is required in Cloudflare mode')
            auth = CloudflareAuth.from_file(Path(path))
        elif mode == 'local':
            path = os.environ.get('WB_CREDENTIALS_FILE')
            if not path:
                raise RuntimeError('WB_CREDENTIALS_FILE is required; no anonymous mode exists')
            auth = Auth.from_file(Path(path))
        else:
            raise RuntimeError('Unknown authentication mode')
    if repository is None:
        path = os.environ.get('WB_DATABASE')
        if not path:
            raise RuntimeError('WB_DATABASE must be an absolute server-local path')
        if not Path(path).is_absolute():
            raise RuntimeError('WB_DATABASE must be absolute')
        repository = SQLiteRepository(Path(path))
    from .report_ai import load_settings as load_ai_settings, start as start_ai
    ai_settings = load_ai_settings()

    @asynccontextmanager
    async def lifespan(app):
        worker = start_ai(repository, ai_settings, settings.zone) if ai_settings else None
        try:
            yield
        finally:
            if worker:
                worker[0].set()

    app = FastAPI(lifespan=lifespan, title='AI Workbench', version='0.2.0', docs_url=None, redoc_url=None, openapi_url=None)
    app.state.repository = repository
    app.state.settings = settings
    app.state.response_preview = ResponsePreviewHub()

    bearer = APIKeyHeader(name='Cf-Access-Jwt-Assertion', auto_error=False, description='Signed identity assertion injected by Cloudflare Access; machine clients authenticate at the edge with service-token headers.') if isinstance(auth, CloudflareAuth) else HTTPBearer(auto_error=False)

    def principal(request: Request, credentials=Depends(bearer)):
        return auth.authenticate(request)

    def operator(who=Depends(principal)):
        if who.role != 'operator':
            raise Problem(403, 'operator_required', 'Collectors cannot commit or alter actions')
        return who

    def collector(who=Depends(principal)):
        if who.role != 'collector':
            raise Problem(403, 'collector_required', 'Only collectors can submit provider observations')
        return who

    @app.exception_handler(Problem)
    async def problem_handler(request, exc):
        return JSONResponse(status_code=exc.status, content={'error': {'code': exc.code, 'message': exc.message}},
                            headers={'WWW-Authenticate': 'Bearer'} if exc.status == 401 else {})

    @app.exception_handler(HTTPException)
    async def http_error_handler(request, exc):
        return JSONResponse(status_code=exc.status_code, content={'error': {'code': 'http_error', 'message': str(exc.detail)}}, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request, exc):
        # Do not echo submitted values (potential secrets) into error responses.
        return JSONResponse(status_code=422, content={'error': {'code': 'validation_error',
            'message': 'Request does not match schema', 'fields': [list(e['loc']) for e in exc.errors()]}})

    @app.middleware('http')
    async def security_headers(request, call_next):
        if request.method in {'POST', 'PATCH', 'PUT', 'DELETE'}:
            origin = request.headers.get('origin')
            expected_origin = os.environ.get('WB_PUBLIC_ORIGIN', str(request.base_url)).rstrip('/')
            if (origin and origin.rstrip('/') != expected_origin) or (request.url.path.startswith('/ui/') and not origin):
                return JSONResponse(status_code=403, content={'error': {'code': 'origin_rejected', 'message': 'Cross-origin writes are disabled'}})
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.get('/healthz')
    def health():
        return {'status': 'ok'}

    @app.get('/openapi.json', dependencies=[Depends(operator)])
    def schema():
        return app.openapi()

    @app.get('/api/reports/{kind}', dependencies=[Depends(operator)])
    def report_data(kind: Literal['accomplishments', 'standup', 'todo']):
        from .reports import report
        return report(repository, kind, zone=settings.zone)

    @app.post('/api/reports/{kind}/suggestions/refresh', dependencies=[Depends(operator)])
    def refresh_report_suggestions(kind: Literal['dashboard', 'standup', 'accomplishments', 'todo']):
        if not ai_settings:
            raise Problem(503, 'generation_unconfigured', 'Report generation is not configured')
        import time
        with repository.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR IGNORE INTO report_suggestions(kind) VALUES (?)', (kind,))
            row = db.execute('SELECT lease_until FROM report_suggestions WHERE kind=?', (kind,)).fetchone()
            if row['lease_until'] > time.time():
                db.commit()
                return {'status': 'generating'}
            db.execute('UPDATE report_suggestions SET next_attempt=0,snapshot_hash=NULL,failure=0 WHERE kind=?', (kind,))
            db.commit()
        return {'status': 'queued', 'note': 'The background worker will refresh this report; prior output is retained.'}

    @app.get('/api/dashboard', dependencies=[Depends(operator)])
    def dashboard():
        return repository.dashboard()

    @app.get('/api/attention', dependencies=[Depends(operator)])
    def attention():
        return repository.dashboard()['attention']

    @app.get('/api/browser/workspaces', dependencies=[Depends(operator)])
    def browser_workspaces():
        from starforge_workbench.browser import load_config
        return load_config()

    @app.post('/api/browser/workspaces/{workspace}/entries', status_code=201)
    def browser_workspace_add(workspace: str, body: BrowserWorkspaceEntryIn, who=Depends(operator)):
        from starforge_workbench.browser import add_entry
        return add_entry(body.name, body.url, workspace, body.match)

    @app.patch('/api/browser/workspaces/{workspace}/entries/{name}')
    def browser_workspace_update(workspace: str, name: str, body: BrowserWorkspacePatch, who=Depends(operator)):
        from starforge_workbench.browser import update_entry
        return update_entry(name, body.url, body.name, workspace, body.match)

    @app.delete('/api/browser/workspaces/{workspace}/entries/{name}')
    def browser_workspace_remove(workspace: str, name: str, who=Depends(operator)):
        from starforge_workbench.browser import remove_entry
        return remove_entry(name, workspace)

    # Named request/response resources keep OpenAPI useful to an LLM client.
    def list_endpoint(table):
        def endpoint(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0), who=Depends(operator)):
            return {'items': repository.list(table, limit, offset), 'limit': limit, 'offset': offset}
        return endpoint

    def get_endpoint(table):
        def endpoint(identity: str, who=Depends(operator)):
            return repository.get(table, identity)
        return endpoint

    for table in ['objectives', 'actions', 'events', 'runs', 'artifacts', 'collectors', 'import_batches', 'import_records']:
        app.add_api_route('/api/'+table, list_endpoint(table), methods=['GET'], name='list_'+table)
        app.add_api_route('/api/'+table+'/{identity}', get_endpoint(table), methods=['GET'], name='get_'+table)

    @app.get('/api/registered-sessions')
    def registered_sessions(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0), who=Depends(operator)):
        return {'items': repository.list_registered_sessions(limit, offset), 'limit': limit, 'offset': offset}

    @app.get('/api/registered-sessions/{identity}')
    def registered_session(identity: str, who=Depends(operator)):
        return repository.get_registered_session(identity)

    @app.post('/api/registered-sessions', status_code=201)
    def register_session(body: RegisteredSessionIn, who=Depends(collector)):
        return repository.register_session(body.model_dump(mode='json'), who.name)

    @app.post('/api/instructions/claim')
    def claim_instruction(body: InstructionClaimIn, who=Depends(collector)):
        return repository.claim_instruction(body.registered_session_id, who.name,
                                            instruction_claims_enabled)

    @app.post('/api/instructions/{identity}/renew')
    def renew_instruction(identity: str, body: InstructionLeaseIn, who=Depends(collector)):
        return repository.renew_instruction_claim(identity, body.lease_token, who.name,
                                                   instruction_claims_enabled)

    @app.post('/api/instructions/{identity}/results')
    def instruction_result(identity: str, body: InstructionResultIn, who=Depends(collector)):
        return repository.report_instruction_result(identity, body.model_dump(mode='json'), who.name)

    @app.post('/api/instructions/{identity}/response-preview', status_code=202)
    async def instruction_response_preview(identity: str, body: InstructionPreviewIn,
                                           who=Depends(collector)):
        # Proves the caller currently holds this instruction's lease; nothing
        # about the excerpt itself is written anywhere. A failed lease check
        # raises the same 404/409 as /results so the worker can tell apart
        # "not mine" from "no one is watching" (a 202 with delivered=false).
        from starlette.concurrency import run_in_threadpool
        target = await run_in_threadpool(
            repository.verify_instruction_lease, identity, body.lease_token, who.name)
        # ResponsePreviewHub owns asyncio queues and must be published from the
        # application event loop, never from FastAPI's sync worker thread.
        delivered = app.state.response_preview.publish(
            target['instruction_id'], target['registered_session_id'],
            body.outcome, body.excerpt)
        return {'status': 'relayed' if delivered else 'dropped'}

    @app.get('/api/instructions/preview-stream', dependencies=[Depends(operator)])
    async def instruction_preview_stream(request: Request):
        import asyncio
        import json as _json
        from fastapi.responses import StreamingResponse
        queue = app.state.response_preview.subscribe()

        async def events():
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ': keep-alive\n\n'
                        continue
                    yield 'data: ' + _json.dumps(event) + '\n\n'
            finally:
                app.state.response_preview.unsubscribe(queue)

        return StreamingResponse(events(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})

    @app.get('/api/instructions')
    def instructions(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                     registered_session_id: str | None = None, who=Depends(operator)):
        return {'items': repository.list_instructions(limit, offset, registered_session_id),
                'limit': limit, 'offset': offset}

    @app.get('/api/instructions/{identity}')
    def instruction(identity: str, who=Depends(operator)):
        return repository.get_instruction(identity)

    @app.post('/api/instructions', status_code=201)
    def create_instruction(body: InstructionIn, who=Depends(operator)):
        return repository.create_instruction(body.model_dump(mode='json'), who.name)

    @app.post('/api/objectives', status_code=201)
    def objective(body: ObjectiveIn, who=Depends(operator)):
        return repository.create('objectives', body.model_dump(mode='json'), who.name)

    @app.post('/api/actions', status_code=201)
    def action(body: ActionIn, who=Depends(principal)):
        if body.execution_mode == 'human' and 'actor' not in body.model_fields_set:
            body.actor = settings.human_name
        if who.role == 'collector' and body.status not in {'observed', 'proposed'}:
            raise Problem(403, 'operator_required', 'Collectors can only propose actions')
        if body.status not in {'observed', 'proposed', 'accepted'}:
            raise Problem(409, 'invalid_initial_status', 'Create an observation, proposal, or explicitly accepted action')
        return repository.create('actions', body.model_dump(mode='json'), who.name)

    @app.patch('/api/actions/{identity}')
    def edit_action(identity: str, body: ActionPatch, who=Depends(operator)):
        data = body.model_dump(mode='json', exclude_unset=True)
        if any(v is None for k, v in data.items() if k != 'due_date'):
            raise Problem(422, 'invalid_null', 'Only due_date can be cleared')
        return repository.patch('actions', identity, data, who.name)

    @app.post('/api/actions/{identity}/transitions')
    def transition(identity: str, body: Transition, who=Depends(operator)):
        target = {'propose': 'proposed', 'accept': 'accepted', 'start': 'in_progress', 'wait': 'waiting',
                  'request-approval': 'approval_needed', 'approve': 'accepted', 'reject': 'rejected',
                  'complete': 'done', 'cancel': 'canceled'}[body.transition]
        if body.transition == 'approve' and repository.get('actions', identity)['status'] != 'approval_needed':
            raise Problem(409, 'invalid_transition', 'Approve requires approval_needed state')
        return repository.patch('actions', identity, {'version': body.version, 'status': target}, who.name)

    @app.post('/api/events', status_code=201)
    def event(body: EventIn, who=Depends(principal)):
        return repository.create('events', body.model_dump(mode='json'), who.name)

    @app.post('/api/runs', status_code=201)
    def run(body: RunIn, who=Depends(principal)):
        if body.action_id:
            raise Problem(409, 'explicit_link_required', 'Create the run first, then link exact current records')
        if who.role == 'collector' and body.objective_id:
            raise Problem(403, 'operator_required', 'Collectors cannot assign a process generation to work')
        return repository.create('runs', body.model_dump(mode='json'), who.name)

    @app.patch('/api/runs/{identity}')
    def edit_run(identity: str, body: RunPatch, who=Depends(operator)):
        data = body.model_dump(mode='json', exclude_unset=True)
        if data.get('status', 'unknown') is None:
            raise Problem(422, 'invalid_null', 'Run status cannot be null')
        return repository.patch('runs', identity, data, who.name)

    @app.post('/api/actions/{action_id}/runs/{run_id}/link')
    def link_run(action_id: str, run_id: str, body: RunLink, who=Depends(operator)):
        return repository.link_run(action_id, run_id, body.model_dump(mode='json'), who.name)

    @app.get('/api/flow/work-items')
    def flow_work_items(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                        who=Depends(operator)):
        return {'items': repository.list_work_items(limit, offset), 'limit': limit, 'offset': offset}

    @app.get('/api/flow/work-items/{work_item_id}')
    def flow_work_item(work_item_id: str, who=Depends(operator)):
        return repository.get_work_item(work_item_id)

    @app.post('/api/flow/work-items', status_code=201)
    def publish_flow_work_item(body: WorkItemProjectionIn, who=Depends(operator)):
        return repository.publish_work_item(body.model_dump(mode='json'), who.name)

    @app.post('/api/flow/work-items/{work_item_id}/actions/{action_id}/link')
    def link_flow_action(work_item_id: str, action_id: str, body: WorkItemActionLinkIn,
                         who=Depends(operator)):
        return repository.link_work_item_action(work_item_id, action_id,
                                                body.model_dump(mode='json'), who.name)

    @app.post('/api/imports', status_code=201)
    def import_batch(body: ImportBatch, who=Depends(operator)):
        return repository.import_batch(body.model_dump(mode='json'), who.name)

    @app.post('/api/collectors/heartbeat')
    def collector_heartbeat(body: CollectorIn, who=Depends(principal)):
        return repository.collector_heartbeat(body.model_dump(mode='json'), who.name)

    @app.post('/api/provider-attention/generations', status_code=201)
    def provider_generation(body: ProviderGenerationIn, who=Depends(collector)):
        return repository.activate_provider_generation(body.model_dump(mode='json'), who.name)

    @app.post('/api/provider-attention/observations', status_code=201)
    def provider_attention(body: ProviderAttentionIn, who=Depends(collector)):
        return repository.record_provider_attention(body.model_dump(mode='json'), who.name)

    @app.post('/api/artifacts', status_code=201)
    def artifact(body: ArtifactIn, who=Depends(operator)):
        return repository.create('artifacts', body.model_dump(mode='json'), who.name)

    templates = Jinja2Templates(directory=str(Path(__file__).with_name('templates')))

    def session_page(identity, notice=None, error=None, retry_key=None):
        from .session_views import display_instruction, display_session
        item = display_session(repository.get_registered_session(identity), repository)
        history = [display_instruction(repository.get_instruction(row['id']))
                   for row in repository.list_instructions(20, 0, identity)]
        key = retry_key if retry_key and re.fullmatch(r'[A-Za-z0-9._~-]{16,128}', retry_key) else secrets.token_urlsafe(24)
        errors = {
            'target_unavailable': 'The session became stale or offline. Refresh its collector evidence before retrying.',
            'target_uncontrollable': 'This exact registration is not currently controllable.',
            'principal_rate_limited': 'Instruction creation is temporarily rate limited. Wait before retrying.',
            'session_rate_limited': 'This session is temporarily rate limited. Wait before retrying.',
            'idempotency_conflict': 'This send attempt no longer matches its original instruction. Start a new attempt.',
            'invalid_instruction': 'Enter 1 to 2,000 supported text characters and confirm the exact session.',
        }
        return item, history, key, errors.get(error), notice == 'queued'

    @app.get('/sessions', response_class=HTMLResponse, dependencies=[Depends(operator)])
    def sessions_view(request: Request, limit: int = Query(100, ge=1, le=100), offset: int = Query(0, ge=0)):
        from .session_views import display_session
        rows = repository.list_registered_sessions(limit + 1, offset)
        items = [display_session(row, repository) for row in rows[:limit]]
        return templates.TemplateResponse(request=request, name='sessions.html', context={
            'sessions': items, 'limit': limit, 'offset': offset, 'has_next': len(rows) > limit})

    @app.get('/sessions/{identity}', response_class=HTMLResponse, dependencies=[Depends(operator)])
    def session_view(request: Request, identity: str, notice: str | None = None,
                     error: str | None = None, retry: str | None = None):
        item, history, key, error_message, sent = session_page(identity, notice, error, retry)
        return templates.TemplateResponse(request=request, name='session.html', context={
            'session': item, 'instructions': history, 'idempotency_key': key,
            'error_message': error_message, 'sent': sent})

    @app.get('/ui/sessions/{identity}/instructions', response_class=HTMLResponse)
    def instruction_timeline(request: Request, identity: str, who=Depends(operator)):
        from .session_views import display_instruction
        repository.get_registered_session(identity)
        history = [display_instruction(repository.get_instruction(row['id']))
                   for row in repository.list_instructions(20, 0, identity)]
        return templates.TemplateResponse(request=request, name='instruction_timeline.html', context={
            'session_id': identity, 'instructions': history})

    @app.post('/ui/sessions/{identity}/instructions')
    def send_session_instruction(request: Request, identity: str, text: str = Form(''), expiry_minutes: str = Form('15'),
                                 idempotency_key: str = Form(''), confirmed: str = Form(''),
                                 who=Depends(operator)):
        def redirect(url):
            if request.headers.get('HX-Request') == 'true':
                return HTMLResponse('', headers={'HX-Redirect': url})
            return RedirectResponse(url, status_code=303)

        key = idempotency_key if re.fullmatch(r'[A-Za-z0-9._~-]{16,128}', idempotency_key) else secrets.token_urlsafe(24)
        try:
            if confirmed != 'yes':
                raise ValueError('confirmation required')
            body = InstructionIn(idempotency_key=key, registered_session_id=identity,
                                 text=text, expiry_minutes=expiry_minutes)
            repository.create_instruction(body.model_dump(mode='json'), who.name)
        except (ValidationError, ValueError):
            return redirect(f'/sessions/{identity}?error=invalid_instruction&retry={key}')
        except Problem as exc:
            safe = exc.code if exc.code in {'target_unavailable', 'target_uncontrollable',
                                             'principal_rate_limited', 'session_rate_limited',
                                             'idempotency_conflict'} else 'invalid_instruction'
            retry = '' if safe == 'idempotency_conflict' else f'&retry={key}'
            return redirect(f'/sessions/{identity}?error={safe}{retry}')
        return redirect(f'/sessions/{identity}?notice=queued')

    @app.get('/reports/{kind}', response_class=HTMLResponse, dependencies=[Depends(operator)])
    def report_view(request: Request, kind: Literal['accomplishments', 'standup', 'todo']):
        from .reports import report
        return templates.TemplateResponse(request=request, name='report.html', context={'report': report(repository, kind, zone=settings.zone)})

    @app.get('/', response_class=HTMLResponse, dependencies=[Depends(operator)])
    def view(request: Request):
        return templates.TemplateResponse(request=request, name='dashboard.html', context={'dashboard': repository.dashboard()})

    @app.get('/guide', response_class=HTMLResponse, dependencies=[Depends(operator)])
    def everyday_guide(request: Request):
        return templates.TemplateResponse(request=request, name='guide.html', context={})

    @app.get('/assets/workbench-logo.png')
    def workbench_logo():
        return FileResponse(Path(__file__).with_name('static')/'workbench-logo.png', media_type='image/png')

    @app.get('/assets/task-form.js')
    def task_form_script():
        return FileResponse(Path(__file__).with_name('static')/'task-form.js', media_type='application/javascript')

    @app.get('/assets/session-form.js')
    def session_form_script():
        return FileResponse(Path(__file__).with_name('static')/'session-form.js', media_type='application/javascript')

    @app.get('/assets/htmx.min.js')
    def htmx():
        return FileResponse(Path(__file__).with_name('static')/'htmx.min.js', media_type='application/javascript')

    @app.get('/assets/response-preview.js')
    def response_preview_script():
        return FileResponse(Path(__file__).with_name('static')/'response-preview.js', media_type='application/javascript')

    @app.get('/ui/dashboard', response_class=HTMLResponse, dependencies=[Depends(operator)])
    def dashboard_fragment(request: Request):
        return templates.TemplateResponse(request=request, name='sections.html', context={'dashboard': repository.dashboard()})

    @app.post('/ui/actions', response_class=HTMLResponse)
    def create_human_action(title: str = Form(...), details: str = Form(''), who=Depends(operator)):
        from pydantic import ValidationError
        try:
            body = ActionIn(title=title, details=details, execution_mode='human', actor=settings.human_name, status='accepted')
        except ValidationError:
            raise Problem(422, 'validation_error', 'Provide a title of 1–500 characters and details under 10000 characters') from None
        repository.create('actions', body.model_dump(mode='json'), who.name)
        return HTMLResponse('<p role="status">Human action created.</p>', headers={'HX-Trigger': 'dashboardChanged'})

    @app.post('/ui/actions/{identity}/complete', response_class=HTMLResponse)
    def complete_human_action(identity: str, version: int = Form(...), who=Depends(operator)):
        repository.patch('actions', identity, {'version': version, 'status': 'done'}, who.name)
        return HTMLResponse('<p role="status">Completed.</p>', headers={'HX-Trigger': 'dashboardChanged'})

    return app
