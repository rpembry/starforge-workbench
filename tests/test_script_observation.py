"""Synthetic contract tests; no configured host script is executed."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import signal
import subprocess
import sys

from jsonschema import Draft202012Validator

from workbench.script_observation import ScriptIdentity, read_status, rgb_cue, run_observed


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
    assert rgb_cue(events[-1])["cue_id"] == "script.process_failed"


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
    current = read_status(events, received_at=now, now=now, freshness_seconds=60)
    assert current["state"] == "process_exit_zero"
    assert current["domain_outcome"] == current["human_acceptance"] == "unknown"
    stale = read_status(events[:1], received_at=now - timedelta(seconds=61), now=now,
                        freshness_seconds=60)
    assert stale["state"] == stale["freshness"] == "unknown"
    assert read_status([], received_at=None, now=now, freshness_seconds=60)["state"] == "unknown"


def test_rgb_seam_has_no_delivery_or_device_configuration():
    events = []
    run_observed(IDENTITY, [sys.executable, "-c", "pass"], events.append)
    assert rgb_cue(events[0]) is None
    cue = rgb_cue(events[1])
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


def test_term_is_forwarded_to_direct_child():
    fixture = """\
import json, sys
from workbench.script_observation import ScriptIdentity, run_observed
def publish(event):
    print(json.dumps(event), flush=True)
raise SystemExit(run_observed(ScriptIdentity('example-script', 'r1', 'local-adapter'),
    [sys.executable, '-c', 'import time; time.sleep(30)'], publish))
"""
    wrapper = subprocess.Popen([sys.executable, "-c", fixture], stdout=subprocess.PIPE, text=True)
    try:
        started = json.loads(wrapper.stdout.readline())
        assert started["kind"] == "started"
        wrapper.send_signal(signal.SIGTERM)
        output, _ = wrapper.communicate(timeout=5)
        assert wrapper.returncode == 143
        assert json.loads(output.strip())["data"]["signal"] == signal.SIGTERM
    finally:
        if wrapper.poll() is None:
            wrapper.kill()
            wrapper.wait()
