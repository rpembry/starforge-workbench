"""Synthetic per-attempt file transport; no Docker daemon or Unix socket."""

import hashlib
import json
import os
from pathlib import Path
import time

import pytest

from coordinator.worker_channel import MailboxChannel
from coordinator.worker_sdk import MailboxWorkerClient


def mailbox(tmp_path):
    attempt = tmp_path / "attempt"
    attempt.mkdir(mode=0o700)
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    channel = MailboxChannel(attempt, job_id="job", attempt_id="attempt", incarnation="first")
    client = MailboxWorkerClient(str(attempt / "channel"), str(scratch / "sender.json"),
                                 job_id="job", attempt_id="attempt", incarnation="first")
    return attempt, scratch, channel, client


def wait_for(predicate):
    end = time.monotonic() + 2
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("mailbox did not reach expected state")


def test_mailbox_ack_follows_durable_host_inbox_and_restart_replays(tmp_path):
    attempt, scratch, channel, client = mailbox(tmp_path)
    try:
        assert client.send("hello", {"capabilities": []})["status"] == "accepted"
        assert (attempt / "host-inbox" / "worker-inbox.json").is_file()
        original = channel.inbox.state["events"][0]["frame"]
        channel.close()
        client.state["seq"] = 0
        client.state["pending"] = original
        client._save()  # ack lost after host fsync, before sender state commit
        channel = MailboxChannel(attempt, job_id="job", attempt_id="attempt", incarnation="first")
        resumed = MailboxWorkerClient(str(attempt / "channel"), str(scratch / "sender.json"),
                                      job_id="job", attempt_id="attempt", incarnation="first")
        assert resumed.retry_pending()["status"] == "duplicate"
        assert channel.inbox.state["last_seq"] == 1
        assert resumed.state["pending"] is None
        assert not (attempt / "channel" / "request.json").exists()
        assert not (attempt / "channel" / "ack.json").exists()
    finally:
        channel.close()


def test_mailbox_pending_survives_host_absence_and_partial_file(tmp_path):
    attempt = tmp_path / "attempt"
    attempt.mkdir(mode=0o700)
    channel_dir = attempt / "channel"
    channel_dir.mkdir(mode=0o700)
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    (channel_dir / ".request-interrupted").write_bytes(b'{"partial":')
    client = MailboxWorkerClient(str(channel_dir), str(scratch / "sender.json"),
                                 job_id="job", attempt_id="attempt", incarnation="first")
    with pytest.raises(ConnectionError, match="pending frame retained"):
        client.send("hello", {"capabilities": []}, max_wait_seconds=0)
    pending = client.state["pending"]
    assert pending["seq"] == 1 and client.state["seq"] == 0
    channel = MailboxChannel(attempt, job_id="job", attempt_id="attempt", incarnation="first")
    try:
        resumed = MailboxWorkerClient(str(channel_dir), str(scratch / "sender.json"),
                                      job_id="job", attempt_id="attempt", incarnation="first")
        assert resumed.state["pending"] == pending
        assert resumed.retry_pending()["status"] == "accepted"
        assert channel.inbox.state["last_seq"] == 1
        assert (channel_dir / ".request-interrupted").is_file()
    finally:
        channel.close()


def test_mailbox_rejects_symlink_hardlink_oversize_and_wrong_attempt(tmp_path):
    attempt, _, channel, _ = mailbox(tmp_path)
    request = attempt / "channel" / "request.json"
    outside = tmp_path / "outside.json"
    wire = (json.dumps({"protocol": "worker.v1", "job_id": "other", "attempt_id": "attempt",
                        "incarnation": "first", "seq": 1, "event_id": "one",
                        "kind": "hello", "data": {"capabilities": []}}) + "\n").encode()
    try:
        outside.write_bytes(wire)
        outside.chmod(0o600)
        request.symlink_to(outside)
        time.sleep(0.12)
        assert channel.inbox.state["last_seq"] == 0
        request.unlink()
        os.link(outside, request)
        time.sleep(0.12)
        assert channel.inbox.state["last_seq"] == 0
        request.unlink()
        request.write_bytes(b"x" * 16_385)
        request.chmod(0o600)
        time.sleep(0.12)
        assert channel.inbox.state["last_seq"] == 0
        request.unlink()
        request.write_bytes(wire)
        request.chmod(0o600)
        wait_for(lambda: (attempt / "channel" / "ack.json").exists())
        assert channel.inbox.state["last_seq"] == 0  # identity rejected
        assert json.loads((attempt / "channel" / "ack.json").read_text())["response"] == {
            "status": "rejected"}
    finally:
        channel.close()


def test_mailbox_inode_race_does_not_delete_replacement(tmp_path):
    attempt, _, channel, _ = mailbox(tmp_path)
    try:
        request = attempt / "channel" / "request.json"
        request.write_text("old")
        old_inode = request.stat().st_ino
        replacement = attempt / "channel" / "new"
        replacement.write_text("new")
        os.replace(replacement, request)
        channel._remove_request(old_inode)
        assert request.read_text() == "new"
    finally:
        channel.close()


def test_mailbox_ack_symlink_cannot_write_outside_and_is_not_host_evidence(tmp_path):
    attempt, scratch, channel, client = mailbox(tmp_path)
    try:
        outside = tmp_path / "outside"
        outside.write_text("untouched")
        ack = attempt / "channel" / "ack.json"
        ack.symlink_to(outside)
        assert client.send("hello", {"capabilities": []})["status"] == "accepted"
        assert outside.read_text() == "untouched"
        assert channel.inbox.state["last_seq"] == 1
    finally:
        channel.close()

    # A worker can fabricate its own ack because the mount is writable. Such
    # bytes never enter the separate host inbox or establish host readiness.
    second = tmp_path / "second"
    second.mkdir(mode=0o700)
    (second / "channel").mkdir(mode=0o700)
    (second / "scratch").mkdir(mode=0o700)
    fake_client = MailboxWorkerClient(str(second / "channel"),
                                      str(second / "scratch" / "sender.json"),
                                      job_id="job", attempt_id="second", incarnation="first")
    with pytest.raises(ConnectionError):
        fake_client.send("hello", {"capabilities": []}, max_wait_seconds=0)
    pending = fake_client.state["pending"]
    wire = (json.dumps(pending, separators=(",", ":")) + "\n").encode()
    (second / "channel" / "ack.json").write_text(json.dumps({
        "seq": pending["seq"], "event_id": pending["event_id"],
        "frame_sha256": hashlib.sha256(wire).hexdigest(),
        "response": {"status": "accepted", "ack_seq": pending["seq"]}}) + "\n")
    (second / "channel" / "ack.json").chmod(0o600)
    assert fake_client.retry_pending()["status"] == "accepted"
    assert not (second / "host-inbox").exists()
