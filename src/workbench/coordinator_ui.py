"""Explicit, local owner browser view over the versioned coordinator socket API."""
from __future__ import annotations

import re
import secrets
from pathlib import Path

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.templating import Jinja2Templates

from coordinator.client import CoordinatorClient, CoordinatorError
from coordinator.contracts import JobSpec


KEY = re.compile(r"[A-Za-z0-9._~-]{16,128}\Z")
ID = re.compile(r"[a-f0-9]{32}\Z")


def client_for(socket: str):
    return CoordinatorClient(socket)


def _error(exc: CoordinatorError) -> str:
    if exc.status == 409:
        return "Version or idempotency conflict. Refresh the job before starting a different action."
    if exc.status == 503:
        return "Coordinator unavailable. The result is unknown; retry this exact request with the same key."
    if exc.status == 404:
        return "Job or evidence was not found."
    if exc.status == 422:
        return "The coordinator rejected the request. Check the registered policy keys and values."
    return f"Coordinator error ({exc.code}). The result is not confirmed."


def install(app, operator, socket: str, *, factory=None):
    """Mount only when the caller has explicitly opted in to a private socket."""
    if not Path(socket).is_absolute():
        raise RuntimeError("WB_COORDINATOR_UI_SOCKET must be absolute")
    factory = factory or client_for
    templates = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))

    @app.middleware("http")
    async def local_browser_only(request: Request, call_next):
        local_hosts = {"localhost", "127.0.0.1", "::1", "testserver"}
        local_clients = {"localhost", "127.0.0.1", "::1", "testclient"}
        forwarded = any(name in request.headers for name in (
            "forwarded", "x-forwarded-for", "x-forwarded-host", "cf-connecting-ip"))
        if (request.url.path.startswith("/coordinator") or
                request.url.path.startswith("/ui/coordinator")) and (
                    request.url.hostname not in local_hosts or
                    not request.client or request.client.host not in local_clients or forwarded):
            return JSONResponse({"error": {"code": "local_only"}}, status_code=403)
        return await call_next(request)

    def page(request, **context):
        return templates.TemplateResponse(request=request, name="coordinator.html", context=context)

    def call(method, *args, **kwargs):
        with factory(socket) as client:
            return getattr(client, method)(*args, **kwargs)

    @app.get("/coordinator", response_class=HTMLResponse, dependencies=[Depends(operator)])
    async def overview(request: Request):
        try:
            health = call("health")
            capabilities = call("capabilities")
            capacity = call("capacity")
            jobs = call("list", 100)["jobs"]
            error = None
        except CoordinatorError as exc:
            health = capabilities = capacity = None
            jobs = []
            error = _error(exc)
        return page(request, mode="list", health=health, capabilities=capabilities,
                    capacity=capacity, jobs=jobs, error=error,
                    submit_key=secrets.token_urlsafe(24))

    @app.get("/coordinator/jobs/{job_id}", response_class=HTMLResponse, dependencies=[Depends(operator)])
    async def detail(request: Request, job_id: str, cursor: int = 0):
        if not ID.fullmatch(job_id):
            return HTMLResponse("Invalid job ID", status_code=404)
        try:
            job = call("get", job_id)["job"]
            recovery = call("recovery", job_id)
            capabilities = call("capabilities")
            capacity = call("capacity")
            events = call("events", job_id, after_seq=max(0, cursor), limit=50)
            error = None
        except CoordinatorError as exc:
            job = recovery = capabilities = capacity = events = None
            error = _error(exc)
        return page(request, mode="detail", job=job, recovery=recovery,
                    capabilities=capabilities, capacity=capacity, events=events, error=error,
                    action_keys={name: secrets.token_urlsafe(24) for name in ("cancel", "retry", "reattach")})

    @app.get("/ui/coordinator/jobs/{job_id}/status", dependencies=[Depends(operator)])
    async def job_status(job_id: str):
        if not ID.fullmatch(job_id):
            return JSONResponse({"error": "invalid_job_id"}, status_code=404)
        try:
            job = call("get", job_id)["job"]
        except CoordinatorError as exc:
            return JSONResponse({"error": exc.code}, status_code=exc.status)
        return {field: job[field] for field in ("id", "version", "phase", "intent", "outcome",
                                                 "visibility", "reason", "updated_at")}

    @app.get("/assets/coordinator-poll.js", dependencies=[Depends(operator)])
    async def coordinator_poll_script():
        return FileResponse(Path(__file__).with_name("static") / "coordinator-poll.js",
                            media_type="application/javascript")

    @app.post("/ui/coordinator/validate", response_class=HTMLResponse)
    async def validate(request: Request, spec: str = Form(""), who=Depends(operator)):
        try:
            parsed = JobSpec.model_validate_json(spec)
            result = call("validate", parsed)
            message = "Valid registered job spec. Submission still requires an explicit action."
            limits = result.get("capacity")
            submit_key = secrets.token_urlsafe(24)
        except (ValueError, CoordinatorError) as exc:
            message = _error(exc) if isinstance(exc, CoordinatorError) else "Invalid job spec JSON or fields."
            limits = None
            submit_key = None
        return page(request, mode="validation", message=message, limits=limits,
                    spec=spec, submit_key=submit_key)

    @app.post("/ui/coordinator/submit", response_class=HTMLResponse)
    async def submit(request: Request, spec: str = Form(""), key: str = Form(""),
               confirmed: str = Form(""), who=Depends(operator)):
        if confirmed != "yes" or not KEY.fullmatch(key) or len(spec.encode()) > 100_000:
            return HTMLResponse("Invalid explicit submission", status_code=422)
        try:
            parsed = JobSpec.model_validate_json(spec)
        except ValueError:
            return HTMLResponse("Invalid job spec", status_code=422)
        try:
            result = call("submit", parsed, key)
            return page(request, mode="result", result=result, message="Submission recorded by coordinator.")
        except CoordinatorError as exc:
            return page(request, mode="uncertain", message=_error(exc),
                        action="submit", key=key, spec=spec, job_id=None, version=None,
                        retryable=exc.status == 503)

    @app.post("/ui/coordinator/jobs/{job_id}/{action}", response_class=HTMLResponse)
    async def mutate(request: Request, job_id: str, action: str, version: int = Form(...),
               key: str = Form(""), confirmed: str = Form(""), who=Depends(operator)):
        if (not ID.fullmatch(job_id) or action not in {"cancel", "retry", "reattach"}
                or version < 1 or confirmed != "yes" or not KEY.fullmatch(key)):
            return HTMLResponse("Invalid explicit action", status_code=422)
        try:
            result = call(action, job_id, version, key)
            return page(request, mode="result", result=result,
                        message=f"{action.capitalize()} request recorded. Operation state is shown below.")
        except CoordinatorError as exc:
            return page(request, mode="uncertain", message=_error(exc),
                        action=action, key=key, spec=None, job_id=job_id, version=version,
                        retryable=exc.status == 503)

    @app.get("/coordinator/jobs/{job_id}/attempts/{attempt_id}/evidence",
             response_class=HTMLResponse, dependencies=[Depends(operator)])
    async def evidence(request: Request, job_id: str, attempt_id: str, cursor: int = 0):
        if not ID.fullmatch(job_id) or not ID.fullmatch(attempt_id) or cursor < 0:
            return HTMLResponse("Invalid evidence identity", status_code=404)
        try:
            job = call("get", job_id)["job"]
            attempt = next((a for a in job["attempts"] if a["id"] == attempt_id), None)
            if not attempt or not attempt["stopped"]:
                return HTMLResponse("Post-stop evidence is not available yet", status_code=409)
            error = None
        except CoordinatorError as exc:
            return page(request, mode="evidence", job_id=job_id, attempt_id=attempt_id,
                        logs=None, artifacts=None, log_error=None, artifact_error=None,
                        error=_error(exc))
        try:
            logs = call("logs", job_id, attempt_id, cursor=cursor, limit=4096)
            log_error = None
        except CoordinatorError as exc:
            logs, log_error = None, _error(exc)
        try:
            artifacts = call("artifacts", job_id, attempt_id)
            artifact_error = None
        except CoordinatorError as exc:
            artifacts, artifact_error = None, _error(exc)
        return page(request, mode="evidence", job_id=job_id, attempt_id=attempt_id,
                    logs=logs, artifacts=artifacts, log_error=log_error,
                    artifact_error=artifact_error, error=error)
