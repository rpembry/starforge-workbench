"""Local v1 control API. Runtime evidence is supplied by an explicit adapter."""
from __future__ import annotations

from typing import Any, Protocol

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import Conflict, CoordinatorStore, JobSpec, Unavailable


class EvidenceAdapter(Protocol):
    """Implement against the reviewed supervisor/worker contract, never Docker directly."""

    def health(self) -> dict[str, Any]: ...
    def capabilities(self) -> dict[str, Any]: ...
    def reconcile(self, job: dict[str, Any], operation_key: str) -> dict[str, Any]: ...
    def logs(self, job: dict[str, Any], attempt_id: str, cursor: int, limit: int) -> dict[str, Any]: ...
    def artifacts(self, job: dict[str, Any], attempt_id: str) -> dict[str, Any]: ...
    def artifact(self, job: dict[str, Any], attempt_id: str, artifact_id: str,
                 offset: int, limit: int) -> dict[str, Any]: ...

class RequestBodyLimit:
    """Bound ASGI input before FastAPI parses it, even without Content-Length."""

    def __init__(self, app, *, limit: int = 131_072):
        self.app = app
        self.limit = limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        for name, raw in scope.get("headers", []):
            if name.lower() == b"content-length":
                try:
                    if int(raw) > self.limit:
                        return await JSONResponse({"code": "oversized_request"}, status_code=413)(
                            scope, receive, send)
                except ValueError:
                    return await JSONResponse({"code": "invalid_length"}, status_code=400)(
                        scope, receive, send)
        chunks = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.limit:
                return await JSONResponse({"code": "oversized_request"}, status_code=413)(
                    scope, receive, send)
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        return await self.app(scope, replay, send)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Submit(Strict):
    spec: JobSpec
    idempotency_key: str = Field(min_length=1, max_length=128)


class Mutation(Strict):
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=128)


def create_app(store: CoordinatorStore, *, adapter: EvidenceAdapter | None = None,
               principal: str = "local-owner") -> FastAPI:
    app = FastAPI(title="Local Coordinator", version="1.0.0", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        # UDS ownership is the normal authentication boundary. Never accept a
        # browser origin or public TCP host through this owner-only API.
        if request.headers.get("origin") or request.headers.get("sec-fetch-site") in {"cross-site", "same-site"}:
            return JSONResponse({"code": "forbidden_origin"}, status_code=403)
        if request.url.hostname not in {"localhost", "127.0.0.1", "::1", "coordinator", "testserver"}:
            return JSONResponse({"code": "nonlocal_host"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(Conflict)
    async def conflict(_request: Request, exc: Conflict):
        return JSONResponse({"code": "conflict", "message": str(exc)}, status_code=409)

    @app.exception_handler(Unavailable)
    async def unavailable(_request: Request, exc: Unavailable):
        return JSONResponse({"code": "unavailable", "message": str(exc)}, status_code=503)

    @app.exception_handler(ValueError)
    async def invalid(_request: Request, exc: ValueError):
        return JSONResponse({"code": "invalid_request", "message": str(exc)}, status_code=422)

    @app.exception_handler(KeyError)
    async def missing(_request: Request, _exc: KeyError):
        return JSONResponse({"code": "not_found"}, status_code=404)

    def evidence() -> EvidenceAdapter:
        if adapter is None:
            raise HTTPException(503, detail={"code": "supervisor_unavailable"})
        return adapter

    @app.get("/v1/health")
    async def health():
        try:
            store.list(1)
            store_state = "ready"
        except Unavailable:
            store_state = "unavailable"
        return {"api": "ready", "store": store_state, "supervisor": adapter.health() if adapter else {"state": "unavailable"},
                "runtime_visibility": "unknown"}

    @app.get("/v1/capabilities")
    async def capabilities():
        worker = adapter.capabilities() if adapter else {}
        return {"api_version": "v1", "jobs": True,
                "reattach": bool(worker.get("reattach", False)),
                "logs": bool(worker.get("post_stop_logs", False)),
                "artifacts": bool(worker.get("post_stop_artifacts", False)),
                "checkpoint_resume": False, "worker": worker}

    @app.get("/v1/capacity")
    async def capacity():
        jobs = store.list(1000)
        if len(jobs) == 1000:
            return {"limits": store.policy.limits.model_dump(),
                    "reservations": {"state": "unknown", "reason": "bounded read reached 1000 jobs"}}
        active = [job for job in jobs if job["phase"] in {"active", "finalizing"}]
        return {"limits": store.policy.limits.model_dump(), "reservations": {
            "state": "snapshot", "pending": sum(job["phase"] == "queued" and job["intent"] == "run" for job in jobs),
            "active": len(active),
            "cpu_millis": sum(job["spec"]["resources"]["cpu_millis"] for job in active),
            "memory_mb": sum(job["spec"]["resources"]["memory_mb"] for job in active)}}

    @app.post("/v1/jobs/validate")
    async def validate(spec: JobSpec):
        try:
            store.policy.validate_spec(spec)
        except ValueError as exc:
            raise HTTPException(422, detail={"code": "invalid_plan", "message": str(exc)}) from exc
        return {"valid": True, "spec": spec.model_dump(mode="json"), "capacity": store.policy.limits.model_dump()}

    @app.post("/v1/jobs", status_code=201)
    async def submit(body: Submit):
        try:
            job = store.submit(body.spec, principal=principal, key=body.idempotency_key)
        except Conflict:
            raise
        except ValueError as exc:
            raise HTTPException(422, detail={"code": "invalid_plan", "message": str(exc)}) from exc
        return {"job": job, "operation": {"key": body.idempotency_key, "state": "confirmed"}}

    @app.get("/v1/jobs")
    async def jobs(limit: int = 100):
        # List is a summary surface. The detailed, socket-protected job route
        # carries the opaque payload and approved policy references.
        fields = ("id", "version", "intent", "phase", "outcome", "visibility",
                  "reason", "attempt_id", "created_at", "updated_at")
        return {"jobs": [{field: job[field] for field in fields}
                         for job in store.list(limit)]}

    @app.get("/v1/jobs/{job_id}")
    async def job(job_id: str):
        return {"job": store.get(job_id)}

    @app.get("/v1/jobs/{job_id}/attempts")
    async def attempts(job_id: str):
        return {"job_id": job_id, "attempts": store.get(job_id)["attempts"]}

    @app.get("/v1/jobs/{job_id}/events")
    async def events(job_id: str, after_seq: int = 0, limit: int = 100):
        return store.replay(job_id, after_seq, limit)

    @app.post("/v1/jobs/{job_id}/cancel")
    async def cancel(job_id: str, body: Mutation):
        result = store.cancel(job_id, expected_version=body.expected_version,
                              principal=principal, key=body.idempotency_key)
        return {"job": result, "operation": {"key": body.idempotency_key,
                "state": "confirmed" if result["phase"] == "terminal" else "pending"}}

    @app.post("/v1/jobs/{job_id}/retry")
    async def retry(job_id: str, body: Mutation):
        result = store.retry(job_id, expected_version=body.expected_version,
                             principal=principal, key=body.idempotency_key)
        return {"job": result, "operation": {"key": body.idempotency_key, "state": "pending"}}

    @app.get("/v1/jobs/{job_id}/recovery")
    async def recovery(job_id: str):
        result = store.get(job_id)
        return {"job_id": job_id, "attempt_id": result["attempt_id"],
                "visibility": result["visibility"], "phase": result["phase"],
                "reattach_available": adapter is not None}

    @app.post("/v1/jobs/{job_id}/reattach")
    async def reattach(job_id: str, body: Mutation):
        if not adapter or not adapter.capabilities().get("reattach", False):
            raise HTTPException(503, detail={"code": "supervisor_unavailable"})
        operation = store.reattach(job_id, expected_version=body.expected_version,
                                   principal=principal, key=body.idempotency_key)
        return {"job": store.get(job_id), "operation": {
            "id": operation["operation_id"], "key": body.idempotency_key,
            "attempt_id": operation["attempt_id"], "state": operation["outcome"]}}

    def checked_attempt(job_id: str, attempt_id: str):
        result = store.get(job_id)
        if not any(a["id"] == attempt_id for a in result["attempts"]):
            raise KeyError(attempt_id)
        return result

    @app.get("/v1/jobs/{job_id}/attempts/{attempt_id}/logs")
    async def logs(job_id: str, attempt_id: str, cursor: int = 0, limit: int = 4096):
        if cursor < 0 or not 1 <= limit <= 65_536:
            raise HTTPException(422, detail={"code": "invalid_cursor"})
        result = checked_attempt(job_id, attempt_id)
        return evidence().logs(result, attempt_id, cursor, limit)

    @app.get("/v1/jobs/{job_id}/attempts/{attempt_id}/artifacts")
    async def artifacts(job_id: str, attempt_id: str):
        result = checked_attempt(job_id, attempt_id)
        return evidence().artifacts(result, attempt_id)

    @app.get("/v1/jobs/{job_id}/attempts/{attempt_id}/artifacts/{artifact_id}")
    async def artifact(job_id: str, attempt_id: str, artifact_id: str,
                       offset: int = 0, limit: int = 65_536):
        if (not artifact_id.isascii() or artifact_id in {".", ".."} or
                not artifact_id.replace("-", "").replace("_", "").replace(".", "").isalnum() or
                len(artifact_id) > 64):
            raise HTTPException(422, detail={"code": "invalid_artifact_id"})
        if offset < 0 or not 1 <= limit <= 65_536:
            raise HTTPException(422, detail={"code": "invalid_artifact_range"})
        result = checked_attempt(job_id, attempt_id)
        return evidence().artifact(result, attempt_id, artifact_id, offset, limit)

    app.add_middleware(RequestBodyLimit)
    return app
