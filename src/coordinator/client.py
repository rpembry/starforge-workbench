"""Typed owner-side transport for the coordinator's protected Unix socket."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
from typing import Any

import httpx

from .contracts import JobSpec


@dataclass(frozen=True)
class CoordinatorError(Exception):
    code: str
    status: int
    message: str = ""

    def __str__(self) -> str:
        return f"{self.code}: {self.message}" if self.message else self.code


class CoordinatorClient:
    def __init__(self, socket: str | Path, *, timeout: float = 10,
                 transport: httpx.BaseTransport | None = None):
        if not 0 < timeout <= 60:
            raise ValueError("timeout must be between 0 and 60 seconds")
        self._directory_fd = None
        if transport is None:
            path = Path(socket).absolute()
            try:
                self._directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                address = f"/proc/self/fd/{self._directory_fd}/{path.name}"
            except OSError:
                # Keep missing-socket errors in the request path so normal CLI
                # commands return the stable unavailable exit code.
                address = str(path)
            try:
                transport = httpx.HTTPTransport(uds=address)
            except BaseException:
                if self._directory_fd is not None:
                    os.close(self._directory_fd)
                raise
        try:
            self._http = httpx.Client(base_url="http://coordinator", timeout=timeout,
                                      transport=transport)
        except BaseException:
            if self._directory_fd is not None:
                os.close(self._directory_fd)
                self._directory_fd = None
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        try:
            self._http.close()
        finally:
            if self._directory_fd is not None:
                os.close(self._directory_fd)
                self._directory_fd = None

    def _call(self, method: str, path: str, *, json: Any = None, params: dict | None = None) -> dict:
        try:
            response = self._http.request(method, path, json=json, params=params)
        except httpx.TransportError as exc:
            # A reset may occur after the server committed the command. Never
            # invent a fresh key or auto-repeat a mutation here.
            raise CoordinatorError("unavailable", 503,
                                   "coordinator response uncertain; reconcile with the same key") from exc
        if response.is_error:
            try:
                body = response.json()
            except ValueError:
                body = {}
            detail = body.get("detail", body)
            if not isinstance(detail, dict):
                detail = {}
            raise CoordinatorError(detail.get("code", "http_error"), response.status_code,
                                   detail.get("message", ""))
        try:
            return response.json()
        except ValueError as exc:
            raise CoordinatorError("unavailable", 503,
                                   "coordinator response invalid; reconcile with the same key") from exc

    def health(self) -> dict:
        return self._call("GET", "/v1/health")

    def capabilities(self) -> dict:
        return self._call("GET", "/v1/capabilities")

    def capacity(self) -> dict:
        return self._call("GET", "/v1/capacity")

    def validate(self, spec: JobSpec) -> dict:
        return self._call("POST", "/v1/jobs/validate", json=spec.model_dump(mode="json"))

    def submit(self, spec: JobSpec, key: str) -> dict:
        return self._call("POST", "/v1/jobs", json={"spec": spec.model_dump(mode="json"), "idempotency_key": key})

    def list(self, limit: int = 100) -> dict:
        return self._call("GET", "/v1/jobs", params={"limit": limit})

    def get(self, job_id: str) -> dict:
        return self._call("GET", f"/v1/jobs/{job_id}")

    def attempts(self, job_id: str) -> dict:
        return self._call("GET", f"/v1/jobs/{job_id}/attempts")

    def events(self, job_id: str, *, after_seq: int = 0, limit: int = 100) -> dict:
        return self._call("GET", f"/v1/jobs/{job_id}/events", params={"after_seq": after_seq, "limit": limit})

    def recovery(self, job_id: str) -> dict:
        return self._call("GET", f"/v1/jobs/{job_id}/recovery")

    def _mutate(self, job_id: str, command: str, expected_version: int, key: str) -> dict:
        return self._call("POST", f"/v1/jobs/{job_id}/{command}",
                          json={"expected_version": expected_version, "idempotency_key": key})

    def cancel(self, job_id: str, expected_version: int, key: str) -> dict:
        return self._mutate(job_id, "cancel", expected_version, key)

    def retry(self, job_id: str, expected_version: int, key: str) -> dict:
        return self._mutate(job_id, "retry", expected_version, key)

    def reattach(self, job_id: str, expected_version: int, key: str) -> dict:
        return self._mutate(job_id, "reattach", expected_version, key)

    def logs(self, job_id: str, attempt_id: str, *, cursor: int = 0, limit: int = 4096) -> dict:
        return self._call("GET", f"/v1/jobs/{job_id}/attempts/{attempt_id}/logs",
                          params={"cursor": cursor, "limit": limit})

    def artifacts(self, job_id: str, attempt_id: str) -> dict:
        return self._call("GET", f"/v1/jobs/{job_id}/attempts/{attempt_id}/artifacts")

    def artifact(self, job_id: str, attempt_id: str, artifact_id: str,
                 *, offset: int = 0, limit: int = 65_536) -> dict:
        return self._call("GET", f"/v1/jobs/{job_id}/attempts/{attempt_id}/artifacts/{artifact_id}",
                          params={"offset": offset, "limit": limit})
