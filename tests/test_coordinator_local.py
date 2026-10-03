"""Local browser boundary tests use synthetic secrets and no network listener."""
import asyncio
import re

import httpx

from test_coordinator_ui import FakeCoordinator, JOB_ID, KEY
from workbench.coordinator_local import create_local_app

ORIGIN = "http://127.0.0.1:8177"
SECRET = "synthetic-one-use-activation-secret-123456789"


def test_activation_session_csrf_and_no_workbench_bearer_bypass():
    fake = FakeCoordinator()
    app = create_local_app("/tmp/fake-coordinator.sock", activation_secret=SECRET,
                           origin=ORIGIN, client_factory=lambda _: fake)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=ORIGIN) as browser:
            assert (await browser.get("/activate")).status_code == 200
            unauthorized = await browser.get("/coordinator", headers={"Authorization": "Bearer " + "x" * 40})
            assert unauthorized.status_code == 401
            assert fake.calls == []
            wrong_origin = await browser.post("/activate", json={"secret": SECRET},
                                              headers={"Origin": "https://public.example"})
            assert wrong_origin.status_code == 403
            oversized = await browser.post("/activate", content=b"x" * 140_000,
                                           headers={"Origin": ORIGIN})
            assert oversized.status_code == 413
            activated = await browser.post("/activate", json={"secret": SECRET},
                                           headers={"Origin": ORIGIN})
            assert activated.status_code == 200
            assert "httponly" in activated.headers["set-cookie"].lower()
            assert "samesite=strict" in activated.headers["set-cookie"].lower()
            repeated = await browser.post("/activate", json={"secret": SECRET}, headers={"Origin": ORIGIN})
            assert repeated.status_code == 403
            page = await browser.get(f"/coordinator/jobs/{JOB_ID}")
            assert page.status_code == 200
            csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
            action = f"/ui/coordinator/jobs/{JOB_ID}/cancel"
            data = {"version": 7, "key": KEY, "confirmed": "yes"}
            assert (await browser.post(action, data=data, headers={"Origin": ORIGIN})).status_code == 403
            assert (await browser.post(action, data={**data, "csrf_token": csrf},
                                       headers={"Origin": "https://public.example"})).status_code == 403
            assert (await browser.post(action, data={**data, "csrf_token": csrf},
                                       headers={"Origin": ORIGIN})).status_code == 200
            assert fake.calls == [("cancel", JOB_ID, 7, KEY)]
            remote = await browser.get("/coordinator", headers={"Host": "public.example",
                                                               "Authorization": "Bearer " + "x" * 40})
            assert remote.status_code == 403
    asyncio.run(scenario())


def test_activation_and_session_expire_in_memory():
    now = [0]
    def clock():
        return now[0]
    app = create_local_app("/tmp/fake-coordinator.sock", activation_secret=SECRET,
                           origin=ORIGIN, clock=clock, client_factory=lambda _: FakeCoordinator())

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=ORIGIN) as browser:
            now[0] = 301
            assert (await browser.post("/activate", json={"secret": SECRET},
                                       headers={"Origin": ORIGIN})).status_code == 403
        now[0] = 0
        second = create_local_app("/tmp/fake-coordinator.sock", activation_secret=SECRET,
                                  origin=ORIGIN, clock=clock, client_factory=lambda _: FakeCoordinator())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(second), base_url=ORIGIN) as browser:
            assert (await browser.post("/activate", json={"secret": SECRET},
                                       headers={"Origin": ORIGIN})).status_code == 200
            assert (await browser.get("/coordinator")).status_code == 200
            cookie = browser.cookies.get("coord_ui_session")
            now[0] = 8 * 3600 + 1
            assert (await browser.get("/coordinator")).status_code == 401
        restarted = create_local_app("/tmp/fake-coordinator.sock", activation_secret=SECRET,
                                     origin=ORIGIN, clock=clock, client_factory=lambda _: FakeCoordinator())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(restarted), base_url=ORIGIN,
                                     cookies={"coord_ui_session": cookie}) as browser:
            assert (await browser.get("/coordinator")).status_code == 401
    asyncio.run(scenario())


def test_validation_requires_exact_loopback_origin():
    import pytest
    from fastapi import FastAPI
    from workbench.coordinator_ui import install
    with pytest.raises(RuntimeError, match="separate local browser boundary"):
        install(FastAPI(), lambda: True, "/tmp/fake-coordinator.sock")
    with pytest.raises(ValueError):
        create_local_app("/tmp/fake-coordinator.sock", activation_secret=SECRET,
                         origin="https://public.example")
    with pytest.raises(ValueError):
        create_local_app("/tmp/fake-coordinator.sock", activation_secret=SECRET,
                         origin=ORIGIN, workbench_url="https://public.example")
