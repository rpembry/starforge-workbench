"""Synthetic contract tests; no configured host script is executed."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import os
import signal
import subprocess
import sys

import pytest

from jsonschema import Draft202012Validator

from workbench.script_observation import ScriptIdentity, read_status, rgb_cue, run_observed, validate_event


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
