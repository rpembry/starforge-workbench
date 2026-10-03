# Coordinator core v1 (#172, #173)

The `coordinator` package is consumer-neutral. It has no import of `workbench`,
FLOW, providers, Docker, or browser code. `JobSpec` is an opaque, bounded request;
its policy references are local administrator keys, not paths or image choices.
Unknown fields, GPU, devices, secrets, network, and nested delegation are rejected.
The coordinator store owns one job ledger, not human task definitions.

| Owner | Durable authority | Does not infer |
| --- | --- | --- |
| Coordinator | Job and attempt identities, command intent, admission reservations, events, outbox | Human acceptance from process exit |
| Supervisor | Host runtime ownership, controller fencing, deadlines, exact stop | New jobs or human task priority |
| Worker | Bounded progress and result declarations | Runtime control or completion of human work |
| Workbench/CLI | Requests through the common coordinator boundary | Scheduling or direct writes to the job ledger |

`src/coordinator/contracts.py` is the versioned source model; its generated JSON
schema is `schemas/coordinator-job-v1.schema.json`. The first slice exposes a
Python service boundary. A local transport and CLI are the dependent #175 task.
`pending_commands()` returns durable launch/cancel/retry operation identities
for the service loop. `set_command_status()` records delivery uncertainty; it
does not set a job result. The API layer must use the operation ID when calling
the supervisor and reconcile the same attempt after a lost response.
`reattach(job_id, expected_version, principal, key)` records a distinct
same-attempt reconciliation command. It never allocates an attempt, and a
replayed key returns the original logical command after a lost response.

The store requires an existing private, caller-owned state directory, creates a
private SQLite database, checks schema version and integrity on open, and uses
`BEGIN IMMEDIATE` plus full synchronous commits. Submission, cancellation, retry,
admission, events, and projection outbox updates share their transaction. Every
external command carries principal and idempotency key. Same key and body returns
the same logical identity; a different body conflicts. A failed or unavailable
commit does not acknowledge a new job or success. The client must look up the
same key after an uncertain response before sending another command.
An existing submit key replays even if the approval policy subsequently changes;
new submissions still use current policy. A repeated cancellation with a new
key and current version aliases the original cancellation command in schema
v2 rather than adding a second stop operation. Migration from schema v1
preserves existing jobs and commands.

Jobs have desired `run`/`cancel`, phase `queued`/`active`/`finalizing`/`terminal`,
separate outcome and observation freshness. A missing observation leaves the
last known phase and marks visibility `unknown`. An attempt has a distinct
incarnation. Explicit retry requires a terminal predecessor with positive stop
evidence and creates a fresh attempt. A parent reference has no execution
authority. Active and finalizing jobs keep reservations even if observation
goes stale. Pending and aggregate resource limits are transactionally checked;
the limits do not reserve capacity against unrelated host processes.

Lifecycle events and outbox records are durable and monotonically sequenced for
each job. `replay(after_seq)` returns both a snapshot and following events so a
client can resynchronize. V1 keeps all lifecycle events; bounded worker logs use
a separate channel. Event retention compaction and replay floors are future
schema changes and must advertise a gap before deleting any events.

This branch's first slice is not a deployable coordinator. It has no local API,
Docker adapter, or service process yet. The supervisor journal and worker
transport are developed separately so their evidence can be reviewed before
the #175 transport or Workbench client is attached.
