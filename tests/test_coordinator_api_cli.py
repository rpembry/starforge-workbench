"""Synthetic local API and CLI contract checks; no runtime is launched."""
import json

import asyncio
import httpx
import pytest

from coordinator import CoordinatorStore, JobSpec, Limits, Policy
from coordinator.api import create_app
from coordinator import cli
from coordinator.client import CoordinatorError


class LocalAPI:
    def __init__(self, app):
        self.app = app

    def request(self, method, path, **kwargs):
        async def call():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(self.app),
                                         base_url="http://coordinator") as client:
                return await client.request(method, path, **kwargs)
        return asyncio.run(call())

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)


@pytest.fixture
def fixture(tmp_path):
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    policy = Policy(profiles=frozenset({"offline"}), workspaces=frozenset({"scratch"}),
                    worker_types=frozenset({"command"}),
                    limits=Limits(max_pending=4, max_active=2, cpu_millis=2000, memory_mb=512))
    store = CoordinatorStore(root, policy)
    spec = JobSpec(worker_type="command", profile_ref="offline", workspace_ref="scratch",
                   payload={"argv": ["true"]}, deadline_seconds=30,
                   resources={"cpu_millis": 1000, "memory_mb": 128})
    return LocalAPI(create_app(store)), store, spec


def test_submit_replay_conflict_and_cancel(fixture):
    api, store, spec = fixture
    body = {"spec": spec.model_dump(mode="json"), "idempotency_key": "submit-1"}
    first = api.post("/v1/jobs", json=body)
    assert first.status_code == 201
    job = first.json()["job"]
    repeated = api.post("/v1/jobs", json=body)
    assert repeated.json()["job"] == job
    different = dict(body, spec=dict(body["spec"], deadline_seconds=31))
    assert api.post("/v1/jobs", json=different).status_code == 409
    job_id = job["id"]
    assert api.get("/v1/capacity").json()["reservations"]["pending"] == 1
    summary = api.get("/v1/jobs").json()["jobs"][0]
    assert summary["id"] == job_id and "spec" not in summary and "payload" not in summary
    assert api.get(f"/v1/jobs/{job_id}/attempts").json()["attempts"] == []
    cancel = {"expected_version": job["version"], "idempotency_key": "cancel-1"}
    result = api.post(f"/v1/jobs/{job_id}/cancel", json=cancel).json()
    assert result["job"]["outcome"] == "cancelled"
    assert result["operation"]["state"] == "confirmed"
    assert api.post(f"/v1/jobs/{job_id}/cancel", json=cancel).json() == result
    assert api.post(f"/v1/jobs/{job_id}/retry", json={"expected_version": 1,
                    "idempotency_key": "retry-1"}).status_code == 409
    replay = api.get(f"/v1/jobs/{job_id}/events?after_seq=0").json()
    assert [event["kind"] for event in replay["events"]] == ["submitted", "cancel_requested"]
    assert replay["next_cursor"] == 2
    assert api.get(f"/v1/jobs/{job_id}/events?after_seq=2").json()["events"] == []


def test_reject_browser_paths_and_unavailable_evidence(fixture):
    api, store, spec = fixture
    job = api.post("/v1/jobs", json={"spec": spec.model_dump(mode="json"),
                   "idempotency_key": "one"}).json()["job"]
    job_id = job["id"]
    assert api.get("/v1/capacity").json()["reservations"]["pending"] == 1
    assert api.get("/v1/jobs", headers={"Origin": "https://evil.example"}).status_code == 403
    assert api.get("/v1/jobs", headers={"Host": "public.example"}).status_code == 403
    assert api.get("/v1/jobs/absent").status_code == 404
    assert api.get(f"/v1/jobs/{job_id}/recovery").json()["reattach_available"] is False
    assert api.post(f"/v1/jobs/{job_id}/reattach", json={"expected_version": job["version"],
                    "idempotency_key": "reattach-1"}).status_code == 503
    assert api.get(f"/v1/jobs/{job_id}/attempts/foreign/logs").status_code == 404
    assert api.get("/v1/capabilities").json()["checkpoint_resume"] is False
    assert api.post("/v1/jobs/validate", json=dict(spec.model_dump(mode="json"),
                    profile_ref="/tmp/unsafe")).status_code == 422
    assert api.post("/v1/jobs/validate", json=dict(spec.model_dump(mode="json"),
                    payload={"data": "x" * 70_000})).status_code == 422


class APIClient:
    def __init__(self, api):
        self.api = api
    def submit(self, spec, key):
        r = self.api.post("/v1/jobs", json={"spec": spec.model_dump(mode="json"), "idempotency_key": key})
        assert r.status_code == 201
        return r.json()
    def get(self, job_id):
        return self.api.get(f"/v1/jobs/{job_id}").json()
    def cancel(self, job_id, expected_version, key):
        return self.api.post(f"/v1/jobs/{job_id}/cancel", json={"expected_version": expected_version,
                             "idempotency_key": key}).json()


def test_cli_same_api_identity_and_state(fixture, tmp_path, capsys):
    api, store, spec = fixture
    path = tmp_path / "job.json"
    path.write_text(spec.model_dump_json())
    args = cli.parser().parse_args(["--socket", "/unused", "--json", "submit", str(path), "--key", "one"])
    assert cli.run(args, APIClient(api)) == 0
    submitted = json.loads(capsys.readouterr().out)
    job = submitted["job"]
    assert job == api.get(f"/v1/jobs/{job['id']}").json()["job"]
    args = cli.parser().parse_args(["--socket", "/unused", "--json", "cancel", job["id"],
                                    "--expected-version", str(job["version"]), "--key", "cancel"])
    assert cli.run(args, APIClient(api)) == 0
    cancelled = json.loads(capsys.readouterr().out)
    assert cancelled["job"] == api.get(f"/v1/jobs/{job['id']}").json()["job"]


def test_cli_unavailable_exit(capsys):
    assert cli.main(["--socket", "/nonexistent/coord.sock", "--json", "health"]) == 6
    assert "unavailable" in capsys.readouterr().err


def test_evidence_routes_scope_attempt_and_advertise_log_gap(fixture):
    _, store, spec = fixture
    first = store.submit(spec, principal="owner", key="first")
    second = store.submit(spec, principal="owner", key="second")
    admitted = store.admit_next(principal="dispatcher", key="admit-first")
    attempt_id = admitted["attempt_id"]

    class Evidence:
        def health(self):
            return {"state": "ready"}
        def capabilities(self):
            return {"worker_types": ["command"]}
        def reconcile(self, job, operation_key):
            return {"key": operation_key, "state": "unknown"}
        def logs(self, job, attempt_id, cursor, limit):
            return {"job_id": job["id"], "attempt_id": attempt_id, "lines": [],
                    "floor": 5, "gap": cursor < 5, "truncated": True, "next_cursor": 5}
        def artifacts(self, job, attempt_id):
            return {"items": [{"id": "output-1", "sha256": "a" * 64}]}
        def artifact(self, job, attempt_id, artifact_id, offset, limit):
            return {"id": artifact_id, "sha256": "a" * 64, "content_base64": ""}

    api = LocalAPI(create_app(store, adapter=Evidence()))
    path = f"/v1/jobs/{first['id']}/attempts/{attempt_id}"
    logs = api.get(path + "/logs?cursor=0").json()
    assert logs["gap"] and logs["truncated"] and logs["floor"] == 5
    assert api.get(path + "/artifacts").json()["items"][0]["sha256"] == "a" * 64
    assert api.get(path + "/artifacts/output-1").json()["id"] == "output-1"
    other = f"/v1/jobs/{second['id']}/attempts/{attempt_id}"
    assert api.get(other + "/artifacts").status_code == 404
    assert api.get(path + "/artifacts/%2e%2e").status_code in {404, 422}
    assert api.get("/openapi.json").json()["info"]["version"] == "1.0.0"


@pytest.mark.parametrize("failure", ["read", "write"])
def test_transport_reset_after_commit_requires_same_key(fixture, failure):
    import httpx
    from coordinator.client import CoordinatorClient, CoordinatorError

    _, store, spec = fixture
    calls = [0]
    error_type = httpx.ReadError if failure == "read" else httpx.WriteError

    def handler(request):
        calls[0] += 1
        body = json.loads(request.content)
        job = store.submit(spec, principal="local-owner", key=body["idempotency_key"])
        if calls[0] == 1:
            raise error_type("connection reset", request=request)
        return httpx.Response(201, json={"job": job,
                              "operation": {"key": body["idempotency_key"], "state": "confirmed"}})

    with CoordinatorClient("/unused", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(CoordinatorError) as error:
            client.submit(spec, "same-key")
        assert error.value.code == "unavailable"
        assert "same key" in error.value.message
        assert calls[0] == 1  # no hidden automatic retry
        replay = client.submit(spec, "same-key")
    assert replay["job"]["id"] == store.list()[0]["id"]
    assert len(store.list()) == 1 and calls[0] == 2


@pytest.mark.parametrize("error_type", ["ReadError", "WriteError"])
def test_cli_transport_reset_is_structured_unavailable(monkeypatch, tmp_path, capsys, error_type):
    import httpx
    from coordinator.client import CoordinatorClient

    def handler(request):
        raise getattr(httpx, error_type)("reset", request=request)

    client = CoordinatorClient("/unused", transport=httpx.MockTransport(handler))
    monkeypatch.setattr(cli, "CoordinatorClient", lambda *_args, **_kwargs: client)
    path = tmp_path / "job.json"
    path.write_text(JobSpec(worker_type="command", profile_ref="offline",
                            workspace_ref="scratch", payload={"argv": ["true"]},
                            deadline_seconds=30,
                            resources={"cpu_millis": 1000, "memory_mb": 128}).model_dump_json())
    assert cli.main(["--socket", "/unused", "--json", "submit", str(path),
                     "--key", "same-key"]) == 6
    stderr = capsys.readouterr().err
    assert "same key" in stderr and "Traceback" not in stderr


def test_streamed_body_cap_without_or_with_false_length(fixture):
    api, _, _ = fixture

    async def chunks():
        yield b"x" * 70_000
        yield b"y" * 70_000

    streamed = api.post("/v1/jobs/validate", content=chunks(),
                        headers={"Content-Type": "application/json"})
    assert streamed.status_code == 413
    assert streamed.json()["code"] == "oversized_request"
    falsified = api.post("/v1/jobs/validate", content=b"x" * 131_073,
                         headers={"Content-Type": "application/json", "Content-Length": "2"})
    assert falsified.status_code == 413


def test_client_uses_fd_relative_address_for_deep_socket(tmp_path, monkeypatch):
    import os
    import httpx
    from coordinator.client import CoordinatorClient

    parent = tmp_path / ("a" * 55) / ("b" * 55)
    parent.mkdir(parents=True, mode=0o700)
    path = parent / "coord.sock"
    assert len(str(path).encode()) > 108
    seen = {}

    def transport(*, uds):
        seen["address"] = uds
        return httpx.MockTransport(lambda _request: httpx.Response(200, json={"api": "ready"}))

    monkeypatch.setattr(httpx, "HTTPTransport", transport)
    with CoordinatorClient(path) as client:
        assert client.health()["api"] == "ready"
        assert seen["address"].startswith("/proc/self/fd/")
        assert seen["address"].endswith("/coord.sock")
        assert len(seen["address"].encode()) < 108
        held_fd = client._directory_fd
        os.fstat(held_fd)
    with pytest.raises(OSError):
        os.fstat(held_fd)
