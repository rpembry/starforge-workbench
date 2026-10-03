"""Synthetic contract tests; no configured host script is executed."""

from datetime import datetime, timedelta, timezone
import json
import inspect
from pathlib import Path
import os
import signal
import subprocess
import sys

import pytest

from jsonschema import Draft202012Validator

from workbench.script_observation import ScriptIdentity, read_status, rgb_cue, run_observed, validate_event
import workbench.script_observation as observation


IDENTITY = ScriptIdentity("example-script", "r1", "local-adapter")


def test_unmodified_command_streams_and_process_result(capfd):
    events = []
    code = run_observed(IDENTITY, [sys.executable, "-c",
                                   "import sys; print('ordinary output'); sys.exit(4)"], events.append)
    assert code == 4
    assert "ordinary output" in capfd.readouterr().out
    assert [event["kind"] for event in events] == ["started", "exited"]
    assert [event["seq"] for event in events] == [1, 2]
    assert len({event["run_id"] for event in events}) == 1
    assert events[-1]["data"]["exit_code"] == 4
    assert events[-1]["data"]["signal"] is None
    assert rgb_cue(events[-1], identity=IDENTITY)["cue_id"] == "script.process_failed"


def test_reporting_outage_cannot_replace_exit():
    def unavailable(_event):
        raise ConnectionError("fixture outage")

    assert run_observed(IDENTITY, [sys.executable, "-c", "import sys; sys.exit(7)"], unavailable) == 7


def test_wrapper_launch_failure_is_distinct():
    events = []
    assert run_observed(IDENTITY, ["/nonexistent/example.invalid-command"], events.append) == 127
    assert [event["kind"] for event in events] == ["launch_failed"]
    assert events[0]["data"] == {"error": "FileNotFoundError"}


def test_status_preserves_unknown_domain_and_staleness():
    events = []
    assert run_observed(IDENTITY, [sys.executable, "-c", "pass"], events.append) == 0
    now = datetime.now(timezone.utc)
    current = read_status(events, identity=IDENTITY, received_at=now, now=now, freshness_seconds=60)
    assert current["state"] == "process_exit_zero"
    assert current["domain_outcome"] == current["human_acceptance"] == "unknown"
    stale = read_status(events[:1], identity=IDENTITY, received_at=now - timedelta(seconds=61), now=now,
                        freshness_seconds=60)
    assert stale["state"] == stale["freshness"] == "unknown"
    assert read_status([], identity=IDENTITY, received_at=None, now=now, freshness_seconds=60)["state"] == "unknown"


def test_rgb_seam_has_no_delivery_or_device_configuration():
    events = []
    run_observed(IDENTITY, [sys.executable, "-c", "pass"], events.append)
    assert rgb_cue(events[0], identity=IDENTITY) is None
    cue = rgb_cue(events[1], identity=IDENTITY)
    assert cue["correlation_id"] == events[1]["run_id"]
    assert cue["status"] == "completed"
    assert not {"recipient", "device", "color", "pushover_token", "endpoint"} & cue.keys()


def test_emitted_events_match_versioned_schema():
    schema = json.loads((Path(__file__).parents[1] / "schemas/script-observation-v1.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)
    events = []
    run_observed(IDENTITY, [sys.executable, "-c", "pass"], events.append)
    run_observed(IDENTITY, ["/nonexistent/example.invalid-command"], events.append)
    for event in events:
        validator.validate(event)
    bad = dict(events[0], script_id="unexpected source", extra="private")
    assert list(validator.iter_errors(bad))
    contradictory = dict(events[1], data={"exit_code": None, "signal": None, "duration_ms": 1})
    assert list(validator.iter_errors(contradictory))


def test_identity_rejects_unicode_and_raw_events_require_source_binding():
    with pytest.raises(ValueError):
        ScriptIdentity("caf\u00e9", "r1", "local-adapter")
    events = []
    run_observed(IDENTITY, [sys.executable, "-c", "pass"], events.append)
    wrong = ScriptIdentity("other-source", "r1", "local-adapter")
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError):
        read_status(events, identity=wrong, received_at=now, now=now, freshness_seconds=60)
    with pytest.raises(ValueError):
        rgb_cue(events[-1], identity=wrong)
    forged = dict(events[-1], data={"exit_code": 0, "signal": 9, "duration_ms": 1})
    with pytest.raises(ValueError):
        validate_event(forged, IDENTITY)
    with pytest.raises(ValueError):
        read_status([forged], identity=IDENTITY, received_at=now, now=now, freshness_seconds=60)
    with pytest.raises(ValueError):
        rgb_cue(forged, identity=IDENTITY)
    with pytest.raises(ValueError):
        validate_event(dict(events[-1], kind=["exited"]), IDENTITY)


def test_status_orders_utc_times_not_timestamp_strings():
    events = []
    run_observed(IDENTITY, [sys.executable, "-c", "pass"], events.append)
    started = dict(events[0], occurred_at="2026-01-01T12:00:00Z")
    exited = dict(events[1], occurred_at="2026-01-01T12:00:00.100000Z")
    now = datetime.now(timezone.utc)
    status = read_status([started, exited], identity=IDENTITY,
                         received_at=now, now=now, freshness_seconds=60)
    assert status["state"] == "process_exit_zero"


def test_term_after_child_assignment_is_forwarded_once(monkeypatch):
    class FakeChild:
        def __init__(self):
            self.signals = []

        def send_signal(self, signum):
            self.signals.append(signum)

        def wait(self):
            return -signal.SIGTERM

    child = FakeChild()
    monkeypatch.setattr(observation.subprocess, "Popen", lambda *_args, **_kwargs: child)
    source, first_line = inspect.getsourcelines(observation.run_observed)
    handoff_line = first_line + next(i for i, line in enumerate(source)
                                     if "for signum in pending_signals:" in line)
    fired = False

    def trace(frame, event, arg):
        nonlocal fired
        if (event == "line" and not fired and frame.f_code.co_filename == observation.__file__
                and frame.f_lineno == handoff_line):
            fired = True
            signal.raise_signal(signal.SIGTERM)
        return trace

    old_trace = sys.gettrace()
    try:
        sys.settrace(trace)
        assert run_observed(IDENTITY, ["fixture"], lambda _event: None) == 143
    finally:
        sys.settrace(old_trace)
    assert fired
    assert child.signals == [signal.SIGTERM]


def test_term_during_child_creation_is_forwarded_after_assignment(monkeypatch):
    class FakeChild:
        signals = None

        def send_signal(self, signum):
            self.signals.append(signum)

        def wait(self):
            return -signal.SIGTERM

    child = FakeChild()
    child.signals = []

    def popen(*_args, **_kwargs):
        signal.raise_signal(signal.SIGTERM)
        return child

    monkeypatch.setattr(observation.subprocess, "Popen", popen)
    assert run_observed(IDENTITY, ["fixture"], lambda _event: None) == 143
    assert child.signals == [signal.SIGTERM]


def test_term_is_forwarded_to_direct_child():
    fixture = """\
import json, sys
from workbench.script_observation import ScriptIdentity, run_observed
def publish(event):
    print(json.dumps(event), flush=True)
raise SystemExit(run_observed(ScriptIdentity('example-script', 'r1', 'local-adapter'),
    [sys.executable, '-c', 'import time; print("child-ready", flush=True); time.sleep(30)'], publish))
"""
    wrapper = subprocess.Popen([sys.executable, "-c", fixture], stdout=subprocess.PIPE, text=True)
    try:
        lines = [wrapper.stdout.readline().strip() for _ in range(2)]
        assert "child-ready" in lines
        started = json.loads(next(line for line in lines if line.startswith("{")))
        assert started["kind"] == "started"
        wrapper.send_signal(signal.SIGTERM)
        output, _ = wrapper.communicate(timeout=5)
        assert wrapper.returncode == 143
        assert json.loads(output.strip())["data"]["signal"] == signal.SIGTERM
    finally:
        if wrapper.poll() is None:
            wrapper.kill()
            wrapper.wait()


def test_blocked_start_sink_does_not_hold_child_after_term():
    fixture = """\
import json, sys, time
from workbench.script_observation import ScriptIdentity, run_observed
def publish(event):
    if event['kind'] == 'started':
        print('start-blocked', flush=True)
        time.sleep(30)
raise SystemExit(run_observed(ScriptIdentity('example-script', 'r1', 'local-adapter'),
    [sys.executable, '-c', 'import os,time; print("child:"+str(os.getpid()), flush=True); time.sleep(30)'], publish))
"""
    wrapper = subprocess.Popen([sys.executable, "-c", fixture], stdout=subprocess.PIPE, text=True)
    child_pid = None
    try:
        lines = [wrapper.stdout.readline().strip() for _ in range(2)]
        assert "start-blocked" in lines
        child_pid = int(next(line[6:] for line in lines if line.startswith("child:")))
        wrapper.send_signal(signal.SIGTERM)
        wrapper.communicate(timeout=5)
        assert wrapper.returncode == 143
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        if wrapper.poll() is None:
            wrapper.kill()
            wrapper.wait()
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
