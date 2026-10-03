"""Synthetic local wrapper and durable-status fixtures; no configured job runs."""

from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import sys

import pytest

from workbench.script_observation import ScriptIdentity, observed_event
from workbench.script_observation_cli import main
from workbench.script_observation_local import (
    ObservationBufferError, status_from_spool, write_event,
)
import workbench.script_observation_local as local


IDENTITY = ScriptIdentity("example-source", "r1", "local-adapter")


def private_descriptor(tmp_path: Path, *, command=None):
    spool = tmp_path / "observations"
    spool.mkdir(mode=0o700)
    descriptor = tmp_path / "script.json"
    descriptor.write_text(json.dumps({
        "version": 1,
        "identity": {"script_id": IDENTITY.script_id, "revision": IDENTITY.revision,
                     "adapter_id": IDENTITY.adapter_id},
        "argv": command or [sys.executable, "-c", "print('synthetic child output')"],
        "spool_dir": str(spool), "cwd": None,
    }))
    descriptor.chmod(0o600)
    return descriptor, spool


def test_synthetic_command_to_durable_redacted_status(tmp_path, capfd):
    descriptor, spool = private_descriptor(tmp_path)
    assert main(["run", "--config", str(descriptor)]) == 0
    assert "synthetic child output" in capfd.readouterr().out
    assert len(list(spool.glob("*.json"))) == 2
    assert main(["status", "--config", str(descriptor)]) == 0
    status = json.loads(capfd.readouterr().out)
    assert status["state"] == "process_exit_zero"
    assert status["owner"] == "external_scheduler"
    assert status["domain_outcome"] == status["human_acceptance"] == "unknown"
    assert "argv" not in status and "spool_dir" not in status
    assert "synthetic child output" not in json.dumps(status)


def test_exact_replay_and_conflict(tmp_path):
    _, spool = private_descriptor(tmp_path)
    event = observed_event(IDENTITY, "a" * 32, 1, "started", {"pid_known": True})
    write_event(spool, IDENTITY, event)
    write_event(spool, IDENTITY, event)
    assert len(list(spool.glob("*.json"))) == 1
    changed = dict(event, occurred_at="2026-01-01T00:00:00Z")
    with pytest.raises(ObservationBufferError):
        write_event(spool, IDENTITY, changed)


def test_source_mismatch_corruption_and_missing_sequence_are_unknown(tmp_path):
    _, spool = private_descriptor(tmp_path)
    event = observed_event(IDENTITY, "b" * 32, 2, "exited",
                           {"exit_code": 0, "signal": None, "duration_ms": 1})
    write_event(spool, IDENTITY, event)
    now = datetime.now(timezone.utc)
    gap = status_from_spool(spool, IDENTITY, now=now, freshness_seconds=300)
    assert gap["state"] == "unknown" and gap["reporting"] == "unavailable"
    other = ScriptIdentity("other-source", "r1", "local-adapter")
    assert status_from_spool(spool, other, now=now, freshness_seconds=300)["state"] == "unknown"
    (spool / "unexpected.json").write_text("private garbage")
    assert status_from_spool(spool, IDENTITY, now=now, freshness_seconds=300)["reporting"] == "unavailable"


def test_protected_descriptor_and_directory_fail_closed(tmp_path, capfd):
    descriptor, spool = private_descriptor(tmp_path)
    descriptor.chmod(0o644)
    assert main(["run", "--config", str(descriptor)]) == 2
    assert "synthetic child output" not in capfd.readouterr().out
    descriptor.chmod(0o600)
    spool.chmod(0o755)
    assert main(["run", "--config", str(descriptor)]) == 0
    assert "synthetic child output" in capfd.readouterr().out
    assert list(spool.iterdir()) == []
    status = status_from_spool(spool, IDENTITY, now=datetime.now(timezone.utc), freshness_seconds=300)
    assert status["state"] == "unknown" and status["reporting"] == "unavailable"


def test_buffer_limit_does_not_discard_prior_evidence(tmp_path, monkeypatch):
    _, spool = private_descriptor(tmp_path)
    monkeypatch.setattr(local, "MAX_EVENTS", 1)
    first = observed_event(IDENTITY, "c" * 32, 1, "started", {"pid_known": True})
    write_event(spool, IDENTITY, first)
    second = observed_event(IDENTITY, "d" * 32, 1, "started", {"pid_known": True})
    with pytest.raises(ObservationBufferError):
        write_event(spool, IDENTITY, second)
    assert len(list(spool.glob("*.json"))) == 1
    status = status_from_spool(spool, IDENTITY, now=datetime.now(timezone.utc), freshness_seconds=300)
    assert status["state"] == "unknown" and status["reporting"] == "full"


def test_full_buffer_cannot_present_old_success_as_current(tmp_path, monkeypatch):
    _, spool = private_descriptor(tmp_path)
    monkeypatch.setattr(local, "MAX_EVENTS", 2)
    old_run = "1" * 32
    start = observed_event(IDENTITY, old_run, 1, "started", {"pid_known": True})
    exit_event = observed_event(IDENTITY, old_run, 2, "exited",
                                {"exit_code": 0, "signal": None, "duration_ms": 1})
    write_event(spool, IDENTITY, start)
    write_event(spool, IDENTITY, exit_event)
    newer_start = observed_event(IDENTITY, "2" * 32, 1, "started", {"pid_known": True})
    with pytest.raises(ObservationBufferError):
        write_event(spool, IDENTITY, newer_start)
    status = status_from_spool(spool, IDENTITY,
                               now=datetime.now(timezone.utc), freshness_seconds=300)
    assert status["state"] == "unknown"
    assert status["freshness"] == "unknown"
    assert status["reporting"] == "full"
    assert len(list(spool.glob("*.json"))) == 2


def test_retained_archive_and_new_evidence_restore_status(tmp_path, monkeypatch):
    _, spool = private_descriptor(tmp_path)
    monkeypatch.setattr(local, "MAX_EVENTS", 3)
    old_run, new_run = "3" * 32, "4" * 32
    for event in (
        observed_event(IDENTITY, old_run, 1, "started", {"pid_known": True}),
        observed_event(IDENTITY, old_run, 2, "exited",
                       {"exit_code": 0, "signal": None, "duration_ms": 1}),
        observed_event(IDENTITY, new_run, 1, "started", {"pid_known": True}),
    ):
        write_event(spool, IDENTITY, event)
    new_exit = observed_event(IDENTITY, new_run, 2, "exited",
                              {"exit_code": 0, "signal": None, "duration_ms": 1})
    with pytest.raises(ObservationBufferError):
        write_event(spool, IDENTITY, new_exit)
    assert status_from_spool(spool, IDENTITY,
                             now=datetime.now(timezone.utc), freshness_seconds=300)["state"] == "unknown"
    archive = tmp_path / "synthetic-archive"
    archive.mkdir(mode=0o700)
    old_files = [path for path in spool.glob("*.json") if path.name.startswith(old_run)]
    old_bytes = [path.read_bytes() for path in old_files]
    for path in old_files:
        path.rename(archive / path.name)
    write_event(spool, IDENTITY, new_exit)
    status = status_from_spool(spool, IDENTITY,
                               now=datetime.now(timezone.utc), freshness_seconds=300)
    assert status["state"] == "process_exit_zero" and status["reporting"] == "available"
    assert [path.read_bytes() for path in sorted(archive.glob("*.json"))] == [
        payload for _, payload in sorted(zip(old_files, old_bytes))]


def test_missing_directory_is_unknown(tmp_path):
    status = status_from_spool(tmp_path / "missing", IDENTITY,
                               now=datetime.now(timezone.utc), freshness_seconds=300)
    assert status["state"] == "unknown" and status["reporting"] == "unavailable"


def test_status_read_does_not_initialize_empty_spool(tmp_path):
    _, spool = private_descriptor(tmp_path)
    assert list(spool.iterdir()) == []
    status = status_from_spool(spool, IDENTITY,
                               now=datetime.now(timezone.utc), freshness_seconds=300)
    assert status["state"] == "unknown"
    assert list(spool.iterdir()) == []


def test_busy_buffer_returns_unknown_without_waiting(tmp_path):
    _, spool = private_descriptor(tmp_path)
    event = observed_event(IDENTITY, "e" * 32, 1, "started", {"pid_known": True})
    write_event(spool, IDENTITY, event)
    with (spool / ".observation.lock").open("rb") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ObservationBufferError):
            write_event(spool, IDENTITY, event)
        status = status_from_spool(spool, IDENTITY,
                                   now=datetime.now(timezone.utc), freshness_seconds=300)
        assert status["state"] == "unknown" and status["reporting"] == "unavailable"
