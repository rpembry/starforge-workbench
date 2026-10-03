"""The browser adapter is exercised against a fake versioned API; no worker runs."""
from fastapi import FastAPI
import asyncio
import httpx
import json

from coordinator.client import CoordinatorError
from workbench.coordinator_ui import install

JOB_ID = "a" * 32
ATTEMPT_ID = "b" * 32
KEY = "same-request-key-123456"


class FakeCoordinator:
    def __init__(self):
        self.calls = []
        self.unavailable = False
        self.stopped = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def health(self):
        return {"api": "ready", "store": "ready", "supervisor": {"state": "unavailable"}, "runtime_visibility": "unknown"}

    def capabilities(self):
        return {"reattach": True, "logs": True, "artifacts": True}

    def capacity(self):
        return {"limits": {"max_pending": 4, "max_active": 2, "cpu_millis": 2000, "memory_mb": 512},
                "reservations": {"state": "unknown", "reason": "bounded read"}}

    def list(self, limit):
        return {"jobs": [{"id": JOB_ID, "phase": "active", "intent": "run", "outcome": None,
                          "visibility": "unknown", "reason": "observation_unavailable", "attempt_id": ATTEMPT_ID,
                          "version": 7, "updated_at": "now"}]}

    def get(self, job_id):
        assert job_id == JOB_ID
        return {"job": {"id": JOB_ID, "phase": "terminal" if self.stopped else "active", "intent": "run",
                        "outcome": None, "visibility": "unknown", "reason": "observation_unavailable",
                        "version": 7, "updated_at": "now", "attempt_id": ATTEMPT_ID, "spec": {
                        "worker_type": "command", "profile_ref": "offline", "workspace_ref": "scratch",
                        "worker_contract": "worker.v1", "deadline_seconds": 30,
                        "resources": {"cpu_millis": 1000, "memory_mb": 128},
                        "orphan_policy": {"mode": "strict", "grace_seconds": 10,
                                          "max_orphan_seconds": 0}, "consumer_ref": None, "parent_ref": None},
                        "attempts": [{"id": ATTEMPT_ID, "ordinal": 1,
                        "phase": "stopped" if self.stopped else "unknown", "outcome": None,
                        "stopped": self.stopped, "incarnation": "i1", "supervisor_id": None,
                        "runtime_id": None, "observation_seq": 0, "last_observed_at": None,
                        "exit_code": None}]}}

    def recovery(self, job_id):
        return {"reattach_available": True}

    def events(self, job_id, **kwargs):
        return {"events": [], "gap": False, "next_cursor": 0}

    def cancel(self, job_id, version, key):
        self.calls.append(("cancel", job_id, version, key))
        if version != 7:
            raise CoordinatorError("conflict", 409)
        if self.unavailable:
            raise CoordinatorError("unavailable", 503)
        return {"job": self.get(job_id)["job"], "operation": {"key": key, "state": "pending"}}

    def retry(self, job_id, version, key):
        self.calls.append(("retry", job_id, version, key))
        return {"job": self.get(job_id)["job"], "operation": {"key": key, "state": "pending"}}

    def reattach(self, job_id, version, key):
        self.calls.append(("reattach", job_id, version, key))
        return {"job": self.get(job_id)["job"], "operation": {"key": key, "state": "unknown"}}

    def logs(self, job_id, attempt_id, **kwargs):
        self.calls.append(("logs", job_id, attempt_id, kwargs))
        return {"text": "last lines", "source_window": "last_1000_lines", "total": 10,
                "sha256": "c" * 64, "possible_prefix_gap": True, "next_cursor": 10}

    def artifacts(self, job_id, attempt_id):
        self.calls.append(("artifacts", job_id, attempt_id))
        return {"execution_ok": None, "items": [{"path": "output.txt", "bytes": 10, "sha256": "c" * 64}]}

    def validate(self, spec):
        self.calls.append(("validate", spec.profile_ref))
        return {"valid": True, "capacity": {"max_pending": 4, "max_active": 2}}

    def submit(self, spec, key):
        self.calls.append(("submit", spec.profile_ref, key))
        return {"job": self.get(JOB_ID)["job"], "operation": {"key": key, "state": "confirmed"}}


def client(fake):
    app = FastAPI()
    async def operator():
        return True
    install(app, operator, "/tmp/fake-coordinator.sock", factory=lambda _: fake)
    class Browser:
        def request(self, method, path, **kwargs):
            async def run():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                             base_url="http://testserver") as client:
                    return await client.request(method, path, **kwargs)
            return asyncio.run(run())

        def get(self, path, **kwargs):
            return self.request("GET", path, **kwargs)

        def post(self, path, **kwargs):
            return self.request("POST", path, **kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass
    return Browser()


def test_unknown_state_and_version_checked_controls():
    fake = FakeCoordinator()
    with client(fake) as browser:
        listing = browser.get("/coordinator")
        assert listing.status_code == 200
        assert "unknown" in listing.text and "bounded read" in listing.text
        detail = browser.get(f"/coordinator/jobs/{JOB_ID}")
        assert detail.status_code == 200
        assert "Version 7" in detail.text
        assert "Orphan mode: strict" in detail.text
        assert "View bounded post-stop evidence" not in detail.text
        status = browser.get(f"/ui/coordinator/jobs/{JOB_ID}/status")
        assert status.json()["version"] == 7
        assert "spec" not in status.json() and "attempts" not in status.json()
        assert browser.post(f"/ui/coordinator/jobs/{JOB_ID}/cancel", data={"version": 7, "key": KEY,
                            "confirmed": "yes"}).status_code == 200
        assert fake.calls == [("cancel", JOB_ID, 7, KEY)]
        assert browser.post(f"/ui/coordinator/jobs/{JOB_ID}/cancel", data={"version": 7,
                            "key": KEY}).status_code == 422


def test_uncertain_response_repeats_exact_request_and_post_stop_gate():
    fake = FakeCoordinator()
    with client(fake) as browser:
        fake.unavailable = True
        response = browser.post(f"/ui/coordinator/jobs/{JOB_ID}/cancel", data={"version": 7,
                                "key": KEY, "confirmed": "yes"})
        assert "result is unknown" in response.text
        assert f'value="{KEY}"' in response.text
        assert 'name="version" value="7"' in response.text
        denied = browser.get(f"/coordinator/jobs/{JOB_ID}/attempts/{ATTEMPT_ID}/evidence")
        assert denied.status_code == 409
        assert not any(call[0] == "logs" for call in fake.calls)
        fake.stopped = True
        evidence = browser.get(f"/coordinator/jobs/{JOB_ID}/attempts/{ATTEMPT_ID}/evidence")
        assert evidence.status_code == 200
        assert "Earlier output may be missing" in evidence.text
        assert "last lines" in evidence.text
        assert "output.txt" in evidence.text


def test_cancel_back_replay_and_stale_version():
    fake = FakeCoordinator()
    with client(fake) as browser:
        first = browser.post(f"/ui/coordinator/jobs/{JOB_ID}/cancel", data={
            "version": 7, "key": KEY, "confirmed": "yes"})
        assert "Operation:" in first.text and "pending" in first.text
        assert browser.get(f"/coordinator/jobs/{JOB_ID}").status_code == 200
        repeated = browser.post(f"/ui/coordinator/jobs/{JOB_ID}/cancel", data={
            "version": 7, "key": KEY, "confirmed": "yes"})
        assert repeated.status_code == 200
        assert fake.calls[-2:] == [("cancel", JOB_ID, 7, KEY)] * 2
        stale = browser.post(f"/ui/coordinator/jobs/{JOB_ID}/cancel", data={
            "version": 6, "key": "fresh-request-key-123", "confirmed": "yes"})
        assert "Version or idempotency conflict" in stale.text
        assert "Retry same cancel request" not in stale.text


def test_lost_response_replay_and_escaped_evidence():
    fake = FakeCoordinator()
    with client(fake) as browser:
        fake.unavailable = True
        lost = browser.post(f"/ui/coordinator/jobs/{JOB_ID}/cancel", data={
            "version": 7, "key": KEY, "confirmed": "yes"})
        assert "Retry same cancel request" in lost.text
        fake.unavailable = False
        replay = browser.post(f"/ui/coordinator/jobs/{JOB_ID}/cancel", data={
            "version": 7, "key": KEY, "confirmed": "yes"})
        assert "pending" in replay.text
        assert fake.calls[-2:] == [("cancel", JOB_ID, 7, KEY)] * 2
        fake.stopped = True
        fake.logs = lambda *_args, **_kwargs: {"text": "<script>alert(1)</script>", "total": 25,
                                             "next_cursor": 25, "possible_prefix_gap": True}
        fake.artifacts = lambda *_args: {"execution_ok": None,
                                        "items": [{"path": "<img src=x onerror=alert(1)>", "bytes": 25}]}
        evidence = browser.get(f"/coordinator/jobs/{JOB_ID}/attempts/{ATTEMPT_ID}/evidence")
        assert evidence.status_code == 200
        assert "&lt;script&gt;" in evidence.text and "<script>alert(1)</script>" not in evidence.text
        assert "&lt;img" in evidence.text and "<img src=x" not in evidence.text


def test_close_and_reopen_only_reads_same_attempt():
    fake = FakeCoordinator()
    with client(fake) as browser:
        assert browser.get(f"/coordinator/jobs/{JOB_ID}").status_code == 200
    with client(fake) as reopened:
        detail = reopened.get(f"/coordinator/jobs/{JOB_ID}")
        assert detail.status_code == 200
        assert ATTEMPT_ID in detail.text
    assert fake.calls == []


def test_artifact_manifest_remains_visible_when_logs_unavailable():
    fake = FakeCoordinator()
    fake.stopped = True
    def missing_logs(*_args, **_kwargs):
        raise CoordinatorError("unavailable", 503)
    fake.logs = missing_logs
    with client(fake) as browser:
        evidence = browser.get(f"/coordinator/jobs/{JOB_ID}/attempts/{ATTEMPT_ID}/evidence")
        assert evidence.status_code == 200
        assert "Logs: Coordinator unavailable" in evidence.text
        assert "output.txt" in evidence.text


def test_remote_host_rejected_before_socket_access():
    fake = FakeCoordinator()
    with client(fake) as browser:
        response = browser.get("/coordinator", headers={"host": "public.example"})
        assert response.status_code == 403
        forwarded = browser.get("/coordinator", headers={"x-forwarded-for": "203.0.113.20"})
        assert forwarded.status_code == 403
        assert fake.calls == []


def test_submission_requires_confirmation_and_preserves_key():
    fake = FakeCoordinator()
    spec = json.dumps({"worker_type": "command", "profile_ref": "offline", "workspace_ref": "scratch",
                       "payload": {"argv": ["true"]}, "deadline_seconds": 30,
                       "resources": {"cpu_millis": 1000, "memory_mb": 128}})
    with client(fake) as browser:
        checked = browser.post("/ui/coordinator/validate", data={"spec": spec})
        assert "Submission still requires an explicit action" in checked.text
        assert fake.calls == [("validate", "offline")]
        denied = browser.post("/ui/coordinator/submit", data={"spec": spec, "key": KEY})
        assert denied.status_code == 422
        confirmed = browser.post("/ui/coordinator/submit", data={"spec": spec, "key": KEY,
                                  "confirmed": "yes"})
        assert confirmed.status_code == 200
        assert ("submit", "offline", KEY) in fake.calls


def test_main_mount_requires_local_opt_in(monkeypatch, tmp_path):
    from workbench.auth import Auth
    from workbench.main import create_app
    from workbench.repository import SQLiteRepository

    auth = Auth({"operator": "o" * 40, "collector": "c" * 40})
    repo = SQLiteRepository(tmp_path / "workbench.sqlite")
    monkeypatch.delenv("WB_COORDINATOR_UI_SOCKET", raising=False)
    assert "/coordinator" not in {route.path for route in create_app(repo, auth).routes}
    monkeypatch.setenv("WB_COORDINATOR_UI_SOCKET", "/tmp/fake-coordinator.sock")
    monkeypatch.setenv("WB_AUTH_MODE", "local")
    assert "/coordinator" in {route.path for route in create_app(repo, auth).routes}
    monkeypatch.setenv("WB_AUTH_MODE", "cloudflare")
    assert "/coordinator" not in {route.path for route in create_app(repo, auth).routes}
    monkeypatch.setenv("WB_AUTH_MODE", "local")
    monkeypatch.setenv("WB_PUBLIC_ORIGIN", "https://public.example")
    assert "/coordinator" not in {route.path for route in create_app(repo, auth).routes}


def test_two_job_api_cli_ui_identity_parity(tmp_path, capsys):
    from coordinator import CoordinatorStore, JobSpec, Limits, Policy, cli
    from coordinator.api import create_app

    root = tmp_path / "coordinator"
    root.mkdir(mode=0o700)
    store = CoordinatorStore(root, Policy(profiles=frozenset({"offline"}),
        workspaces=frozenset({"scratch"}), worker_types=frozenset({"command"}),
        limits=Limits(max_pending=4, max_active=2, cpu_millis=2000, memory_mb=512)))
    spec = JobSpec(worker_type="command", profile_ref="offline", workspace_ref="scratch",
                   payload={"argv": ["true"]}, deadline_seconds=30,
                   resources={"cpu_millis": 1000, "memory_mb": 128})
    first = store.submit(spec, principal="owner", key="first")
    second = store.submit(spec, principal="owner", key="second")

    async def api_list():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(create_app(store)),
                                     base_url="http://coordinator") as api:
            return (await api.get("/v1/jobs")).json()["jobs"]
    api_ids = {job["id"] for job in asyncio.run(api_list())}

    class StoreClient(FakeCoordinator):
        def list(self, limit):
            fields = ("id", "phase", "intent", "outcome", "visibility", "reason",
                      "attempt_id", "version", "updated_at")
            return {"jobs": [{name: job[name] for name in fields} for job in store.list(limit)]}

    adapter = StoreClient()
    args = cli.parser().parse_args(["--json", "list"])
    assert cli.run(args, adapter) == 0
    cli_ids = {job["id"] for job in json.loads(capsys.readouterr().out)["jobs"]}
    with client(adapter) as browser:
        response = browser.get("/coordinator")
        assert response.status_code == 200
        for identity in api_ids:
            assert identity in response.text
    assert api_ids == cli_ids == {first["id"], second["id"]}
