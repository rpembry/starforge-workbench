"""Owner-reviewed retention of bytes from a positively never-started attempt.

This path copies private source bytes and never removes a worktree, Git
administrative entry, or other original allocation content.
"""

import hashlib
import os
import stat
import tempfile

from starforge_workbench.docker_worker import WorkerError, atomic, private_directory
from starforge_workbench.worker_disposition import fsync_tree, inventory, read_private


ROOTS = ("worktree", "scratch", "git-mask", "channel", "host-inbox", "artifacts", "runtime.json")


def _roots(attempt):
    return {name: attempt / name for name in ROOTS}


def _identity(receipt):
    return {"evidence_kind": "no_start_retention", "attempt_id": receipt["attempt_id"],
            "job_id": receipt["job_id"], "incarnation": receipt["incarnation"],
            "plan_hash": receipt["plan_hash"], "workspace_kind": receipt["workspace_kind"],
            "repository": receipt.get("repository"), "revision": receipt.get("revision"),
            "runtime_id": None}


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source(runtime, plan):
    if not runtime.inspect_no_start(plan):
        raise WorkerError("exact precreate absence required for retention")
    receipt = runtime._read(plan["attempt_id"])
    attempt = private_directory(receipt["attempt_path"])
    allowed = set(ROOTS) | {"no-start-review.json", "no-start-retention.json", "archive"}
    if any(child.name not in allowed for child in attempt.iterdir()):
        raise WorkerError("unrecognized partial allocation content")
    return receipt, attempt


def _snapshot_inventory(target):
    if any(child.name not in ROOTS for child in target.iterdir()):
        raise WorkerError("unexpected retained snapshot content")
    return inventory(_roots(target))


def review(runtime, plan):
    receipt, attempt = _source(runtime, plan)
    review_path = attempt / "no-start-review.json"
    if ((attempt / "no-start-retention.json").exists() or
            (attempt / "no-start-retention.json").is_symlink()):
        raise WorkerError("no-start retention already started")
    entries = inventory(_roots(attempt))
    atomic(review_path, {**_identity(receipt), "receipt_sha256": _digest(attempt / "runtime.json"),
                         "entries": entries})
    return {"attempt_id": receipt["attempt_id"], "evidence_kind": "no_start_retention",
            "review_sha256": _digest(review_path), "review_manifest": str(review_path),
            "entries": len(entries)}


def _copy_reviewed(attempt, target, entries):
    directories = []
    for name, info in sorted(entries.items(), key=lambda pair: (pair[0].count("/"), pair[0])):
        source, destination = attempt / name, target / name
        if source.resolve() != source.absolute():
            raise WorkerError("source path changed during retention")
        if info["kind"] == "directory":
            destination.mkdir(mode=0o700, exist_ok=True)
            if destination.is_symlink() or not destination.is_dir():
                raise WorkerError("retention directory changed")
            destination.chmod(0o700)  # keep writable until every child is copied
            directories.append((destination, info["mode"]))
            continue
        if destination.is_symlink() or destination.is_dir():
            raise WorkerError("retention file changed")
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        temporary = None
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or opened.st_size != info["bytes"]:
                raise WorkerError("reviewed source changed")
            out_fd, temporary = tempfile.mkstemp(prefix=".no-start-copy-", dir=target.parent)
            digest = hashlib.sha256()
            with os.fdopen(fd, "rb") as src, os.fdopen(out_fd, "wb") as dst:
                fd = -1
                remaining = info["bytes"]
                while remaining:
                    chunk = src.read(min(65_536, remaining))
                    if not chunk:
                        raise WorkerError("reviewed source truncated")
                    dst.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
                if src.read(1) or digest.hexdigest() != info["sha256"]:
                    raise WorkerError("reviewed source changed")
                os.fchmod(dst.fileno(), info["mode"])
                dst.flush()
                os.fsync(dst.fileno())
            os.replace(temporary, destination)
            temporary = None
        finally:
            if fd >= 0:
                os.close(fd)
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)
    for directory, mode in reversed(directories):
        directory.chmod(mode)


def retain(runtime, plan, review_sha256, operation_id):
    receipt, attempt = _source(runtime, plan)
    review_path = attempt / "no-start-review.json"
    approved = read_private(review_path)
    if (_digest(review_path) != review_sha256 or
            any(approved.get(key) != value for key, value in _identity(receipt).items()) or
            approved.get("receipt_sha256") != _digest(attempt / "runtime.json")):
        raise WorkerError("no-start review identity or digest changed")
    journal_path = attempt / "no-start-retention.json"
    journal = read_private(journal_path) if journal_path.exists() else None
    if journal:
        if (journal.get("review_sha256") != review_sha256 or
                journal.get("operation_id") != operation_id or
                journal.get("phase") not in {"retaining", "complete"}):
            raise WorkerError("no-start retention operation changed")
    else:
        if inventory(_roots(attempt)) != approved["entries"]:
            raise WorkerError("reviewed source changed")
        if (attempt / "archive").exists() or (attempt / "archive").is_symlink():
            raise WorkerError("unexpected archive path")
        journal = {"phase": "retaining", "operation_id": operation_id,
                   "review_sha256": review_sha256, "evidence_kind": "no_start_retention"}
        atomic(journal_path, journal)
    archive = attempt / "archive"
    if archive.is_symlink():
        raise WorkerError("archive path changed")
    archive.mkdir(mode=0o700, exist_ok=True)
    private_directory(archive)
    target = archive / "no-start"
    if target.is_symlink():
        raise WorkerError("retained snapshot path changed")
    target.mkdir(mode=0o700, exist_ok=True)
    private_directory(target)
    if inventory(_roots(attempt)) != approved["entries"]:
        raise WorkerError("reviewed source changed")
    if journal["phase"] == "complete":
        if _snapshot_inventory(target) != approved["entries"] or journal.get("archive") != str(target):
            raise WorkerError("retained snapshot changed")
        return journal
    _copy_reviewed(attempt, target, approved["entries"])
    if (inventory(_roots(attempt)) != approved["entries"] or
            _snapshot_inventory(target) != approved["entries"] or
            not runtime.inspect_no_start(plan)):
        raise WorkerError("reviewed retention changed")
    fsync_tree(target)
    directory_fd = os.open(archive, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    journal = {**journal, "phase": "complete", "archive": str(target)}
    atomic(journal_path, journal)
    return journal
