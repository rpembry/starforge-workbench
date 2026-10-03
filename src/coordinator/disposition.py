"""Review-bound retention of a stopped coordinator allocation.

This has its own receipt format. Legacy worker receipts are never loaded or
modified. The caller holds the supervisor lock and has committed exit/artifact
evidence before invoking either operation.
"""

import hashlib
import json
import os
from pathlib import Path

from starforge_workbench.docker_worker import WorkerError, atomic, git, private_directory, verify_worktree
from starforge_workbench.worker_disposition import fsync_tree, inventory, read_private, verify_unstaged


ROOTS = ("worktree", "scratch", "artifacts")


def _identity(receipt, runtime_id):
    return {"attempt_id": receipt["attempt_id"], "incarnation": receipt["incarnation"],
            "plan_hash": receipt["plan_hash"], "runtime_id": runtime_id,
            "workspace_kind": receipt["workspace_kind"],
            "repository": receipt.get("repository"), "revision": receipt.get("revision")}


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _roots(attempt, *, archived=False):
    return {name: (attempt / "artifacts" if name == "artifacts" else
                   attempt / "archive" / name if archived else attempt / name)
            for name in ROOTS}


def review(runtime, plan, runtime_id):
    receipt = runtime._read(plan["attempt_id"])
    attempt = private_directory(receipt["attempt_path"])
    if (attempt / "disposition.json").exists():
        raise WorkerError("disposition already started")
    observed = runtime.inspect(plan, runtime_id)
    if not observed["identity_ok"] or not observed["stopped"] or observed["runtime_id"] != runtime_id:
        raise WorkerError("exact stopped runtime required for review")
    if receipt["workspace_kind"] == "git_worktree":
        verify_worktree(receipt)
        verify_unstaged(receipt)
    entries = inventory(_roots(attempt))
    data = {**_identity(receipt, runtime_id), "entries": entries}
    atomic(attempt / "review.json", data)
    return {"attempt_id": receipt["attempt_id"], "review_sha256": _hash(attempt / "review.json"),
            "entries": len(entries), "review_manifest": str(attempt / "review.json")}


def archive_absent(runtime, plan, runtime_id, review_sha256, operation_id):
    """Read-only proof that an approved archive removed its exact runtime."""
    receipt = runtime._read(plan["attempt_id"])
    if (receipt["job_id"] != plan["job_id"] or
            receipt["incarnation"] != plan["incarnation"] or
            receipt["plan_hash"] != hashlib.sha256(
                json.dumps(plan, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            ).hexdigest() or receipt["container_id"] != runtime_id):
        raise WorkerError("archive runtime identity changed")
    attempt = private_directory(receipt["attempt_path"])
    review_path, disposition_path = attempt / "review.json", attempt / "disposition.json"
    if not disposition_path.exists():
        return False
    approved = read_private(review_path)
    disposition = read_private(disposition_path)
    if (_hash(review_path) != review_sha256 or
            any(approved.get(key) != value for key, value in _identity(receipt, runtime_id).items()) or
            disposition.get("runtime_id") != runtime_id or
            disposition.get("review_sha256") != review_sha256 or
            disposition.get("operation_id") != operation_id or
            disposition.get("phase") not in {"archiving", "complete"}):
        raise WorkerError("approved archive identity changed")
    if disposition["phase"] == "archiving":
        if (attempt / "archive").is_symlink():
            raise WorkerError("archive path changed")
        roots = {}
        for name in ROOTS:
            source, target = attempt / name, attempt / "archive" / name
            if name == "artifacts":
                roots[name] = source
            elif source.exists() and target.exists():
                raise WorkerError("ambiguous archive ownership")
            else:
                roots[name] = target if target.exists() else source
    else:
        if disposition.get("archive") != str(attempt / "archive"):
            raise WorkerError("archive destination changed")
        if any((attempt / name).exists() or (attempt / name).is_symlink()
               for name in ("worktree", "scratch")):
            raise WorkerError("disposed allocation reappeared")
        roots = _roots(attempt, archived=True)
    if inventory(roots) != approved["entries"]:
        raise WorkerError("reviewed archive content changed")
    return runtime.inspect_optional(plan, runtime_id) is None


def archive(runtime, plan, runtime_id, review_sha256, operation_id):
    receipt = runtime._read(plan["attempt_id"])
    attempt = private_directory(receipt["attempt_path"])
    review_path, disposition_path = attempt / "review.json", attempt / "disposition.json"
    approved = read_private(review_path)
    if _hash(review_path) != review_sha256 or any(
            approved.get(key) != value for key, value in _identity(receipt, runtime_id).items()):
        raise WorkerError("review identity or digest changed")
    journal = read_private(disposition_path) if disposition_path.exists() else None
    if journal:
        if (journal.get("review_sha256") != review_sha256 or
                journal.get("operation_id") != operation_id or
                journal.get("phase") not in {"archiving", "complete"}):
            raise WorkerError("disposition operation identity changed")
    else:
        observed = runtime.inspect(plan, runtime_id)
        if not observed["identity_ok"] or not observed["stopped"] or observed["runtime_id"] != runtime_id:
            raise WorkerError("exact stopped runtime required for archive")
        if receipt["workspace_kind"] == "git_worktree":
            verify_worktree(receipt)
            verify_unstaged(receipt)
        if inventory(_roots(attempt)) != approved["entries"]:
            raise WorkerError("work changed after review")
        if (attempt / "archive").exists() or (attempt / "archive").is_symlink():
            raise WorkerError("unexpected archive path")
        journal = {"phase": "archiving", "review_sha256": review_sha256,
                   "operation_id": operation_id, "runtime_id": runtime_id}
        atomic(disposition_path, journal)  # intent precedes Docker removal and rename
    if journal["phase"] == "complete":
        if any((attempt / name).exists() or (attempt / name).is_symlink()
               for name in ("worktree", "scratch")):
            raise WorkerError("disposed allocation reappeared")
        if inventory(_roots(attempt, archived=True)) != approved["entries"]:
            raise WorkerError("archived content changed")
        return journal
    archive_dir = attempt / "archive"
    if archive_dir.is_symlink():
        raise WorkerError("archive path changed")
    archive_dir.mkdir(mode=0o700, exist_ok=True)
    private_directory(archive_dir)
    roots = {}
    for name in ROOTS:
        source, target = attempt / name, archive_dir / name
        if name == "artifacts":
            roots[name] = source
        elif source.exists() and target.exists():
            raise WorkerError("ambiguous archive ownership")
        else:
            roots[name] = target if target.exists() else source
    if inventory(roots) != approved["entries"]:
        raise WorkerError("reviewed content changed during archive")
    # Docker removal is exact, and replay after a lost reply verifies absence.
    observed = runtime.inspect_optional(plan, runtime_id)
    if observed is not None:
        if not observed["identity_ok"] or not observed["stopped"] or observed["runtime_id"] != runtime_id:
            raise WorkerError("runtime changed during archive")
        runtime.command(runtime.docker + ["rm", runtime_id])
        if runtime.inspect_optional(plan, runtime_id) is not None:
            raise WorkerError("exact runtime removal unconfirmed")
    for name in ("worktree", "scratch"):
        source, target = attempt / name, archive_dir / name
        if source.exists():
            os.rename(source, target)
    fsync_tree(archive_dir)
    fsync_tree(attempt / "artifacts")
    directory_fd = os.open(attempt, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    if inventory(_roots(attempt, archived=True)) != approved["entries"]:
        raise WorkerError("archive changed; retained for review")
    if receipt["workspace_kind"] == "git_worktree":
        listing = git(receipt["repository"], "worktree", "list", "--porcelain").decode()
        if "worktree " + receipt["worktree"] + "\n" in listing:
            verify_unstaged(receipt, archive_dir / "worktree")
            git(receipt["repository"], "worktree", "remove", receipt["worktree"])
    journal = {**journal, "phase": "complete", "archive": str(archive_dir)}
    atomic(disposition_path, journal)
    return journal
