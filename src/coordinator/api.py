"""Local v1 control API. Runtime evidence is supplied by an explicit adapter."""
from __future__ import annotations

from typing import Any, Protocol

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import Conflict, CoordinatorStore, JobSpec, Unavailable


class EvidenceAdapter(Protocol):
    """Implement against the reviewed supervisor/worker contract, never Docker directly."""

    async def health(self) -> dict[str, Any]: ...
    async def capabilities(self) -> dict[str, Any]: ...
    def reconcile(self, job: dict[str, Any], operation_key: str) -> dict[str, Any]: ...
    async def logs(self, job: dict[str, Any], attempt_id: str, cursor: int, limit: int) -> dict[str, Any]: ...
    async def artifacts(self, job: dict[str, Any], attempt_id: str) -> dict[str, Any]: ...
    async def artifact(self, job: dict[str, Any], attempt_id: str, artifact_id: str) -> dict[str, Any]: ...


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
        if request.headers.get("content-length") and int(request.headers["content-length"]) > 131_072:
            return JSONResponse({"code": "oversized_request"}, status_code=413)
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
                "runtime_visibility": "unknown" if adapter is None else "adapter_reported"}

    @app.get("/v1/capabilities")
    async def capabilities():
        return {"api_version": "v1", "jobs": True, "reattach": adapter is not None,
                "logs": adapter is not None, "artifacts": adapter is not None,
                "checkpoint_resume": False, "worker": adapter.capabilities() if adapter else {}}

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
        result = store.get(job_id)
        if result["version"] != body.expected_version:
            raise Conflict("job version changed")
        # This call must verify exact supervisor ownership and generation. The
        # adapter is intentionally absent until its contract is reviewed.
        return {"job": result, "operation": evidence().reconcile(result, body.idempotency_key)}

    def checked_attempt(job_id: str, attempt_id: str):
        result = store.get(job_id)
        if not any(a["id"] == attempt_id for a in result["attempts"]):
            raise KeyError(attempt_id)
        return result

    @app.get("/v1/jobs/{job_id}/attempts/{attempt_id}/logs")
    async def logs(job_id: str, attempt_id: str, cursor: int = 0, limit: int = 100):
        if cursor < 0 or not 1 <= limit <= 1000:
            raise HTTPException(422, detail={"code": "invalid_cursor"})
        result = checked_attempt(job_id, attempt_id)
        return evidence().logs(result, attempt_id, cursor, limit)

    @app.get("/v1/jobs/{job_id}/attempts/{attempt_id}/artifacts")
    async def artifacts(job_id: str, attempt_id: str):
        result = checked_attempt(job_id, attempt_id)
        return evidence().artifacts(result, attempt_id)

    @app.get("/v1/jobs/{job_id}/attempts/{attempt_id}/artifacts/{artifact_id}")
    async def artifact(job_id: str, attempt_id: str, artifact_id: str):
        if not artifact_id.isascii() or not artifact_id.replace("-", "").replace("_", "").isalnum():
            raise HTTPException(422, detail={"code": "invalid_artifact_id"})
        result = checked_attempt(job_id, attempt_id)
        return evidence().artifact(result, attempt_id, artifact_id)

    return app
