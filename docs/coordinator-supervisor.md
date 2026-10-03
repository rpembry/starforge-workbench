# Host supervisor journal slice (#176)

`coordinator.supervisor.Supervisor` is an independent host boundary. It needs
only a private state directory, clock, and host-owned runtime adapter. The
coordinator database can be absent while `tick()` enforces persisted deadlines
and orphan policy or `owner_stop()` fences the coordinator and stops an exact
attempt. A service wrapper must run the tick loop independently of the
coordinator and authenticate its owner-only local stop endpoint.
The optional `coord-supervisor` entry point runs this process explicitly; it
does not install a service. Example private config:

```json
{"profiles":{"offline":{"backend":"docker","repository_strategy":"per-task-worktree","image":"example.invalid/reviewed@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","toolchain":"fixture","user":1000,"group":1000,"cpus":1,"memory_mb":64,"pids_limit":32,"timeout_seconds":30,"network":"none","mounts":[{"source":"worktree","target":"/workspace","read_only":false}]}},"workspaces":{"scratch":"scratch"}}
```

Create the state directory and config outside Git with mode 0700/0600 and set
the profile UID/GID to the actual non-root service account. Then run
`coord-supervisor serve --state-root DIR --config FILE`. An approved Git
workspace entry uses
`"reviewed_repo":{"kind":"git_worktree","repository":"/absolute/repository","revision":"<full-commit-id>"}`
in `workspaces`; the service state directory must be outside that repository.
The repository and revision are operator configuration, not job input.
Optional `host_budget` has exact integer keys `max_active`, `cpu_millis`, and
`memory_mb`. Without it, the supervisor reserves room for one allocation.
For example, `{"max_active":2,"cpu_millis":2000,"memory_mb":128}` permits
two reviewed 1-CPU/64-MiB profiles only when the daemon reports at least that
capacity. The supervisor sums resolved profile reservations for every
non-stopped attempt under its launch lock; a full budget fails before a new
intent or Docker create. A stopped attempt releases its reservation. The
operator must choose a slice that leaves capacity for other local workloads;
Docker host capacity alone does not measure their current resource use.
Each allocation's resolved reservation is persisted with its launch intent.
Changing or revoking a profile cannot shrink an existing worker's accounting.
An older active journal row without a provable reservation blocks new starts
until it is positively stopped and reconciled.
The service's private Unix
`control.sock` accepts fenced acquire/renew/launch/reconcile/cancel/inspect;
`owner.sock` accepts owner stop, inspect, explicit takeover, `review`, and
`archive`. Review and archive require committed stopped
runtime and artifact evidence. Run `coord-supervisor review --socket
DIR/owner.sock ATTEMPT_ID`, inspect the returned private review manifest, then
run `coord-supervisor archive --socket DIR/owner.sock ATTEMPT_ID REVIEW_SHA256
OPERATION_ID` to retain exact reviewed bytes under the attempt's `archive/`.
The archive operation durably records intent before exact stopped-container
removal and file renames. It keeps `artifacts/` at its evidence path, retains
dirty Git work and scratch bytes, and removes only the old linked-worktree
administrative entry using non-force Git removal after the archive is fsynced
and rechecked. A changed file, unsafe link, ownership mismatch, or uncertain
runtime leaves the allocation retained for review. The same archive operation
ID is replayable after a lost response. The control socket rejects
`owner_takeover`. `coord-supervisor owner-stop --socket
DIR/owner.sock ATTEMPT_ID OPERATION_ID` exits successfully only after confirmed
stop. Both sockets require the same local UID; this first profile trusts that
Unix account. Keep the owner socket path out of ordinary client configuration.
Repeated reconciliation after a committed exact archive preserves the stopped
state after the container is removed; it rechecks the private disposition,
archived content, and exact absence before accepting that terminal evidence.
The tick loop runs every 250 ms independently of coordinator availability and
reports changed uncertainty without unbounded repeated error lines. The
service refuses existing socket paths rather than replacing a possibly live
owner, and it removes only sockets it created on clean shutdown.
The control socket also provides `abandon(plan, controller, generation,
operation_id)` for a durable no-start tombstone, fenced
`collect(attempt_id, controller, generation)` for
committed process/result/artifact evidence, and `read_artifact` for bounded
hash-verified chunks of exported files such as `output.txt` logs. On restart,
new launches remain blocked until every existing attempt has exact, positive
runtime ownership evidence. The owner socket remains available while recovery
is uncertain. Mutating control reconciliation requires the current controller
and generation; a stale coordinator cannot change journal observations.
If Docker starts a worker but its response is lost, cancellation and the
watchdog inspect the exact receipt, token, labels, plan, and incarnation before
adopting its runtime ID for a scoped stop. A mismatched resource stays unknown.

The journal records one active controller lease and monotonically increasing
generation, immutable attempt/operation IDs, a plan hash, runtime identity,
sticky cancellation, original deadline, and orphan deadline. Calls are
serialized across service processes around the authority check and runtime
mutation. A duplicate launch operation returns its journal record and never
calls launch again. If a launch response is lost, reconciliation checks the
exact attempt and runtime identity. It does not create a replacement. Owner
stop fences the current generation before exact runtime stop. An interrupted
stop reports unknown until observation confirms termination.
The watchdog processes every due attempt even when one has mismatched ownership
or missing evidence. It reports which exact attempts stopped and which remain
uncertain; it never stops a resource whose identity failed verification.
It also inspects active attempts for normal process exit without a coordinator
connection. For protocol workers, the host-acknowledged `hello` and `ready`
sequence must be complete within 30 seconds of the durable launch intent (or
the shorter job deadline). A worker still running without that evidence is
stopped by the independent watchdog. A worker that exits before readiness is
recorded as stopped; collection cannot mark it successful without complete
protocol evidence. A failed runtime inspection remains uncertain and does not
authorize a stop of an unverified resource.
If the host inbox cannot be read at the readiness deadline, a worker with a
positively verified running identity is stopped at that deadline and the
readiness observation remains uncertain.

If an owner stop cannot commit to the journal, the owner endpoint reports an
uncertain outcome. While holding the supervisor's cross-process lock, it may
make a best-effort stop only for an attempt with a committed runtime ID and a
fresh positive match of plan, receipt, labels, incarnation, and runtime ID.
No committed ID, unreadable journal, or mismatched runtime means no emergency
mutation. Even a positive post-stop inspection does not become a confirmed
owner stop without durable cancel and fencing evidence. Investigate the
attempt with the owner inspection endpoint after journal service is restored;
do not infer cancellation or retry from the uncertain response.

The default `strict` policy stops at lease expiry plus configured grace, capped
by the original runtime deadline. Explicit `trusted_local` continuation stops
at the earlier original deadline or its persisted orphan budget. Neither
budget is refreshed by a coordinator reconnection. A backward clock step
blocks starts and causes the watchdog to stop owned attempts conservatively.

The runtime adapter contract is in the class docstring. `DockerRuntime` now
implements approved scratch or Git-worktree workspaces for ordinary commands
and the fixed `protocol_example` worker. The Git workspace is bound by private
operator configuration to an absolute repository and full commit, never by a
job-supplied host path or branch. It refuses configured Git content filters,
records a receipt before linked-worktree allocation, verifies the linked
worktree and HEAD, and retains work plus bounded `changes.patch` and
`status.txt` exports for review. A partial worktree setup with no confirmed
Docker runtime remains unknown and retained for explicit recovery; it is not
reported as an execution result. The adapter resolves
symbolic profile/workspace references from administrator configuration, uses
the existing runner's digest-pinned image/environment and restricted create
verification, and checks exact labels/token/ID, plan hash, and incarnation on
every inspect and stop. A runtime mismatch is unknown and is never adopted or
removed blindly. `collect()` exports bounded logs, exit metadata, and declared
regular files after positive stop; the workspace and container remain for
review. No legacy receipt is adopted automatically. The existing Git-worktree
runner remains a separate compatibility path, unchanged by this slice.

This slice contains an independently runnable local service process,
scratch and Git workspace adapters, explicit review/archive disposition, and
fake-Docker tests. Live Docker proof and end-to-end #175 dispatch
integration remain gated. Host death suspends enforcement until the
supervisor restarts and reconciles. The owner and control sockets both trust
the same local UID; they are separate operation surfaces, not isolation from
a malicious process running under that UID. No service installation or live
Docker claim is made by these tests.

Operator evidence remains open for a reviewed, digest-pinned protocol image,
live two-worker admission and runtime measurements, independent resource
reservation against actual host load, and
a fault matrix on a host with ordinary Docker access. The legacy Git runner
remains untouched. Remote access and Starkeep testing belong
to a separately approved later gate.
