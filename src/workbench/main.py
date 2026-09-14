"""FastAPI factory. Start explicitly on 127.0.0.1; no module-import side effects."""
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Form, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBearer, APIKeyHeader
from starlette.exceptions import HTTPException

from .auth import Auth, CloudflareAuth
from .models import (ActionIn, ActionPatch, ArtifactIn, EventIn, ObjectiveIn,
                     RunIn, RunLink, RunPatch, Transition, CollectorIn, ImportBatch,
                     ProviderAttentionIn, ProviderGenerationIn)
from .repository import Problem, SQLiteRepository
from .settings import load_settings


def create_app(repository=None, auth=None, settings=None):
    if settings is None:
        settings = load_settings()
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

    @app.get('/assets/htmx.min.js')
    def htmx():
        return FileResponse(Path(__file__).with_name('static')/'htmx.min.js', media_type='application/javascript')

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
