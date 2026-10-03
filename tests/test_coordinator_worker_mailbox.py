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


def test_mailbox_second_owner_fails_before_loading_inbox(tmp_path, monkeypatch):
    attempt, _, channel, client = mailbox(tmp_path)
    lock_path = attempt / "host-inbox" / "channel.lock"
    lock_inode = lock_path.stat().st_ino
    try:
        client.send("hello", {"capabilities": []})
        client.send("ready", {})
        inbox_path = attempt / "host-inbox" / "worker-inbox.json"
        before = inbox_path.read_bytes()
        assert channel.inbox.state["ready"] is True
        def must_not_load(*args, **kwargs):
            raise AssertionError("second owner loaded inbox")
        with monkeypatch.context() as patch:
            patch.setattr("coordinator.worker_channel.WorkerInbox", must_not_load)
            with pytest.raises(ValueError, match="already served"):
                MailboxChannel(attempt, job_id="job", attempt_id="attempt", incarnation="first")
        assert inbox_path.read_bytes() == before
        assert channel.inbox.state["ready"] is True
        client.send("heartbeat", {})
        assert channel.inbox.state["last_seq"] == 3
    finally:
        channel.close()
    assert lock_path.stat().st_ino == lock_inode
    restarted = MailboxChannel(attempt, job_id="job", attempt_id="attempt", incarnation="first")
    try:
        assert lock_path.stat().st_ino == lock_inode
        assert restarted.inbox.state["last_seq"] == 3
        assert restarted.inbox.state["ready"] is True
        client.send("heartbeat", {})
        assert restarted.inbox.state["last_seq"] == 4
    finally:
        restarted.close()
        restarted.close()  # cleanup cannot close a reused descriptor twice


def test_mailbox_lock_path_rejects_symlink_and_hardlink(tmp_path):
    attempt = tmp_path / "attempt"
    attempt.mkdir(mode=0o700)
    inbox = attempt / "host-inbox"
    inbox.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    lock = inbox / "channel.lock"
    lock.symlink_to(outside)
    with pytest.raises(OSError):
        MailboxChannel(attempt, job_id="job", attempt_id="attempt", incarnation="first")
    assert outside.read_text() == "untouched"
    lock.unlink()
    os.link(outside, lock)
    with pytest.raises(ValueError, match="lock path changed"):
        MailboxChannel(attempt, job_id="job", attempt_id="attempt", incarnation="first")
    assert not (inbox / "worker-inbox.json").exists()


def test_mailbox_failed_start_releases_lock(tmp_path, monkeypatch):
    attempt = tmp_path / "attempt"
    attempt.mkdir(mode=0o700)
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise RuntimeError("inbox load failed")
        patch.setattr("coordinator.worker_channel.WorkerInbox", fail)
        with pytest.raises(RuntimeError, match="inbox load failed"):
            MailboxChannel(attempt, job_id="job", attempt_id="attempt", incarnation="first")
    channel = MailboxChannel(attempt, job_id="job", attempt_id="attempt", incarnation="first")
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
