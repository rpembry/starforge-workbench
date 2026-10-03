"""Worker protocol identity, bounds, replay, and acknowledgement tests."""

import json

import pytest

from coordinator.worker_protocol import Frame, ProtocolError, WorkerInbox, decode_frame


def frame(seq, kind, data=None, **identity):
    return Frame(job_id=identity.get("job_id", "job"), attempt_id=identity.get("attempt_id", "attempt"),
                 incarnation=identity.get("incarnation", "first"), seq=seq,
                 event_id=f"event-{seq}", kind=kind, data=data or {})


@pytest.fixture
def inbox(tmp_path):
    root = tmp_path / "attempt"
    root.mkdir(mode=0o700)
    return WorkerInbox(root, job_id="job", attempt_id="attempt", incarnation="first")


def test_handshake_duplicate_and_result_survive_reconnect(inbox):
    hello = frame(1, "hello", {"capabilities": []})
    with pytest.raises(ProtocolError, match="handshake"):
        inbox.accept(frame(1, "ready"))
    assert inbox.accept(hello)["status"] == "accepted"
    assert inbox.accept(hello)["status"] == "duplicate"
    assert inbox.accept(frame(2, "ready"))["status"] == "accepted"
    result = frame(3, "result", {"ok": True, "summary": "fixture finished"})
    inbox.accept(result)
    restarted = WorkerInbox(inbox.directory, job_id="job", attempt_id="attempt", incarnation="first")
    assert restarted.accept(result)["status"] == "duplicate"  # lost final ack
    assert restarted.replay(1)["result"]["data"]["ok"] is True
    assert restarted.replay(1)["events"][0]["kind"] == "ready"
    with pytest.raises(ProtocolError, match="result already"):
        restarted.accept(frame(4, "result", {"ok": False, "summary": "conflict"}))


def test_stale_incarnation_gap_and_conflict(inbox):
    inbox.accept(frame(1, "hello", {"capabilities": []}))
    with pytest.raises(ProtocolError, match="stale-incarnation"):
        inbox.accept(frame(2, "heartbeat", incarnation="second"))
    accepted = inbox.accept(frame(4, "heartbeat"))
    assert accepted["gap"] == [2, 3]
    assert inbox.replay(1)["sequence_gaps"] == [[2, 3]]
    with pytest.raises(ProtocolError, match="stale or conflicting"):
        inbox.accept(frame(4, "ready"))
    with pytest.raises(ProtocolError, match="stale or conflicting"):
        inbox.accept(frame(3, "ready"))


def test_bounds_and_artifact_rules(inbox):
    with pytest.raises(ProtocolError):
        decode_frame(b"{" + b"x" * 20_000 + b"}\n")
    with pytest.raises(ProtocolError):
        decode_frame(b"not json\n")
    with pytest.raises(ValueError):
        frame(1, "artifact", {"path": "../secret"})
    with pytest.raises(ValueError):
        frame(1, "progress", {"completed": 2, "total": 1})
    inbox.accept(frame(1, "hello", {"capabilities": []}))
    for seq in range(2, 134):
        inbox.accept(frame(seq, "heartbeat"))
    replay = inbox.replay(0)
    assert replay["gap"] and replay["floor"] > 1
    assert len(replay["events"]) <= 128
    with pytest.raises(ProtocolError, match="stale or conflicting"):
        inbox.accept(frame(1, "hello", {"capabilities": []}))


def test_frame_wire_roundtrip():
    value = frame(1, "hello", {"capabilities": []})
    assert decode_frame((value.model_dump_json() + "\n").encode()) == value
