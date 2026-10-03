# Headless worker v1 contract (#174)

The wire protocol is newline-delimited JSON. The fixed example worker exchanges
one request and one acknowledgement through a private **per-attempt** file mailbox.
Older in-flight receipts may still use their per-attempt Unix socket.
The host mounts only that worker's channel. No supervisor owner API or Docker
socket is present in the worker. `Frame` and `WorkerInbox` in
`src/coordinator/worker_protocol.py` define the receiver. `worker_sdk.py` is a
stdlib-only sender that may be copied into a reviewed image. The host must
derive job/attempt/incarnation identity from its journal, never from worker
claims alone.

Every frame has `protocol="worker.v1"`, `job_id`, `attempt_id`,
`incarnation`, monotonic `seq`, stable `event_id`, `kind`, and bounded `data`.
The first frame is `hello` with an explicit capability list. V1 accepts the
reserved `checkpoint` declaration but **does not enable resume**. Basic kinds
are `ready`, `heartbeat`, `progress` (`completed`/`total`), structured `log`
(`level`/`message`), `artifact` (relative path), and `result` (`ok`/`summary`).
Frame size is 16 KiB; data is 8 KiB. The host acknowledges only after fsyncing
the inbox record. The SDK persists a pending frame before sending and retries
the same event ID after a lost acknowledgement.
The sender retries a pending frame for a bounded 30 seconds by default; if the
channel remains unavailable, its exact pending frame stays on disk for an
explicit later retry. Older inboxes reconstruct artifact declarations only
when their complete sequence is retained. A rotated or gapped legacy inbox
keeps success uncertain rather than silently omitting a required artifact.

The inbox retains at most 128 frames and 1 MiB for replay, plus the latest
result record and explicit sequence gaps. A cursor below its floor returns
`gap=true`; consumers must resynchronize from a snapshot. Identical recent
duplicates are acknowledged; changed, stale, cross-attempt, or old-incarnation
frames are rejected. Readiness is a worker acknowledgement, heartbeat is only
liveness, and progress is only a worker report. None proves process exit or
human acceptance. A result declaration alone cannot mark a job successful.

The ordinary-command adapter (`command_evidence`) reports synthetic readiness
only after the command is observed running, no semantic progress, and a result
only from confirmed process exit. Its exit zero still needs host artifact checks
to satisfy an execution contract. Stdout and stderr stay with the existing
bounded Docker log path. Artifact declarations are untrusted hints; the host
must verify containment, symlinks, regular files, byte limits, and hashes.

`examples/coordinator_worker.py` and `examples/Dockerfile.coordinator-worker`
show a tiny scratch-only worker. Build requires an explicitly reviewed,
locally cached Python base image by immutable digest; the Dockerfile does not
pull an image or add credentials. The worker assumes `/workspace/job.json` is
host-prepared bounded input, `/scratch` is its writable area, and `/channel`
contains only its file mailbox. This mailbox requires the worker's numeric UID
to match the controller's UID because its directory and files are mode 0700/0600.
The host controller must set a non-root UID/GID, read-only root, network none,
dropped capabilities, no new privileges, PID/CPU/memory/time limits, and no
container Git. The current
`DockerRuntime` adapter supports ordinary commands and the fixed
`protocol_example` worker only with a scratch workspace. Its job deadline may
not exceed the approved profile timeout. It mounts only that attempt's
file mailbox; inbox state stays in a separate host-only directory. A mailbox
acknowledgement is written only after the host inbox fsyncs the frame; the
worker-writable mailbox itself is never host evidence. Channel ownership uses
a lifetime lock in that host-only directory; normal
channel cleanup leaves the lock inode in place. A trusted host process with
write access to the inbox directory must coordinate before changing it. Host
collection requires positive process exit, readiness, a worker result, and
the declared `result.json` artifact with JSON content matching the bounded
input's `value` before execution success. File-mailbox retry
and fake-Docker tests pass; the Docker mount/entrypoint path and supported
pinned images still require opt-in live runtime verification.

Cancellation is supervisor-owned: it first sends typed cancel when a connected
worker transport supports it or a termination signal, then performs bounded
exact-runtime stop. Worker silence or a success claim cannot veto the stop.
