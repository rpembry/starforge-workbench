# ADR: opt-in Docker worker execution

Status: proposed for review in #48. This document defines a future implementation;
no backend selector, container runner, or API schema change is introduced here.

## Decision and motivation

Keep `tmux` as the default worker backend. Offer `docker` only when explicitly
selected for an authorized task whose reproducible toolchain or runtime isolation
justifies the additional setup. An absent profile preserves today's native path
without probing Docker, pulling images, creating worktrees, or changing sessions.
An explicitly selected but unavailable Docker backend fails before launch; it
never silently runs the task natively. Backend selection is not execution consent.

The current launcher in `src/starforge_workbench/cli.py` owns tmux sessions,
checkout locks and provider bindings. The process collector in
`src/workbench/collector.py` observes those sessions. Neither implements container
execution. Add a small host-side worker backend seam for newly requested tasks;
do not refactor persistent conversation launching merely to make it fit.

Docker may improve dependency isolation, concurrent source editing and repeatable
integration checks. It costs image storage, startup time, cleanup machinery and
operator decisions. Containers share the host kernel and the host-side Docker
controller is privileged. This is a controlled execution option for approved
work, not a claim that arbitrary hostile code is safely contained.

## Selection flow

1. Resolve the authorized task, source revision and explicit execution profile.
2. If backend is omitted or `tmux`, retain native behavior. No Docker dependency.
3. If `docker`, validate the profile and local prerequisites without creating a
   worker: immutable image identity, command, workspace plan, limits, mounts,
   network policy, credential references and artifact destinations.
4. Produce a resolved launch plan. Reject unsupported options and unsupported
   provider authentication before side effects; do not broaden permissions.
5. Allocate an attempt identity and durable ownership receipt, prepare its
   worktree, then create and start the container. Return evidence or a bounded
   failure with recovery ownership, never just an opaque success flag.

Use an internal typed configuration in #49, not a public versioned schema or a
new web/API execution endpoint. No rule automatically decides that a task should
use Docker. Native execution stays selectable even if Docker is installed.

## Backend contract

The names below describe responsibilities, not new implemented Python methods or
HTTP endpoints. #49/#50 can adjust their spelling while preserving semantics.

| Operation | Input | Output and guarantees |
| --- | --- | --- |
| Plan / validate | Authorized task reference; provider/command as an argument vector; resolved profile; repository and immutable base revision; approved source policy; artifact policy | Resolved plan or actionable validation failure; no worker launch and no execution authority implied |
| Start | Plan; unique attempt ID and retry/idempotency key | Owned handle or bounded failure; repeated requests for the same attempt reconcile the same resource, rather than create another worker |
| Inspect | Owned handle and bounded timeout | Runtime state, observation time, evidence source and optional exit status; unavailable inspection means unknown |
| Cancel | Exact owned handle and explicit cancellation request | Bounded graceful stop, then scoped forced stop if needed; repeated requests are safe; never target a name pattern or unrelated process |
| Collect artifacts | Handle; declared relative artifact paths | Private artifact manifest with hashes and completeness/error status; preserve available evidence on failure as well as success |
| Cleanup | Handle; durable receipt; artifact/dirty-work policy | Removed owned resources or explicit retained resources with reason and recovery steps; repeatable without data loss |

The resolved plan includes backend, image digest/toolchain identity, argv,
container working directory, source revision, task worktree strategy, numeric
user/group, CPU/memory/PID limits, timeout, network policy, approved mount map,
credential references (never values), and artifact limits. Do not store a shell
command assembled from untrusted interpolation. Reject an image tag that cannot
be resolved and recorded as a digest before execution. Source defaults to a
committed revision; including uncommitted content requires an explicit snapshot
choice, not a silent copy of the operator's checkout.

The handle records attempt ID, task reference, backend and backend resource ID,
resolved-plan hash, worktree path, ownership token, timestamps and artifact
location. Docker ownership uses container ID plus matching namespaced labels;
name alone is insufficient. Native ownership continues to use existing verified
session/process identity. Keep receipts outside Git with restrictive permissions.
A new attempt gets a new identity, even for the same task.

## State, health and completion

Local attempt states are `planned`, `preparing`, `starting`, `running`,
`exited`, `cancelled`, `start_failed`, and `unknown`. Cleanup has its own
`pending`, `complete`, or `retained` state; it does not erase the outcome.

Record timestamped lifecycle observations for allocation, preparation, start,
exit, cancellation and cleanup. A positive inspection can establish running or
exited; losing daemon access, a timed-out probe, or missing collector evidence
establishes unknown visibility. An unexpectedly missing container is unknown
unless an owned cleanup receipt or reliable exit record explains its absence.
Do not recreate an unknown worker automatically.

Record exit code, signal and reason when available. Exit zero means the command
exited successfully, not that the human's task is verified complete. Artifacts
and acceptance checks supply separate evidence. Process output volume and a
heartbeat do not prove progress. Keep explicitly linked action IDs and worker
attempt IDs separate from provider conversation IDs.

#50 must expose bounded inspection and local lifecycle receipts sufficient for
#54 to measure the runner. Use monotonic time for durations and UTC for event
ordering. The proposed default inspection interval is 15 seconds with a 5-second
probe timeout; evidence older than 90 seconds is stale/unknown. #53, if justified,
adds ongoing collection and richer lifecycle integration. It must preserve the
existing API `RunIn` status vocabulary (`running`, `waiting`, `approval_needed`,
`stopped`, `unknown`); local attempt states are not new API enum values. A future
projection uses verified exit evidence for `stopped` and preserves the exit
result separately. Worker heartbeats never carry operator credentials.

## Responsibilities and failure handling

| Concern | Native tmux | Optional Docker |
| --- | --- | --- |
| Runtime | Existing host provider executable and environment | Digest-recorded image and validated argv; no implicit host environment inheritance |
| Source ownership | Existing checkout locks and selected directory | One host-owned Git worktree per attempt, prepared before launch; concurrent attempts never share writable source |
| Session continuity | Existing exact provider bindings and interactive attach | New task attempt, bounded lifetime; no migration/resume of existing native sessions |
| Runtime observation | Verified host process/session metadata | Host controller inspects exact container identity; observer outage remains distinct from worker failure |
| Failure evidence | Existing launcher diagnostics | Preserve receipt, bounded logs and exit/creation evidence before removal; report partial preparation explicitly |
| Cleanup | Retain persistent native conversations under existing policy | Stop/remove only owned containers; retain changed work or incomplete artifacts explicitly; remove clean owned worktrees when safe |

Write the ownership receipt before each resource-creation step. If a response is
lost after container creation, reconcile labels and attempt identity before
retrying. If the controller crashes, the next explicit recovery operation finds
owned resources from the receipt and labels. Unknown ownership blocks deletion.
This minimal reconciliation belongs in #50; a background recovery service and
restart/resume scheduler do not.

Cancellation or startup failure may still leave useful edits. Collect the diff,
exit information and declared artifacts first. If artifact export fails or edits
remain unpreserved, retain the worktree with a clear cleanup-pending receipt.
Never force-remove a dirty worktree merely to meet a cleanup metric. Never use
Docker prune, broad container-name matching, or delete shared images/caches as
per-task cleanup. Retained resources are accounted-for work, not silent leaks.

## Boundaries required before the first real trial

- The controller runs on the host. Neither the host Docker socket nor a remote
  Docker endpoint, host control-plane credentials or host sudo access enters a
  worker. Passwordless Docker on an operator workstation is a local deployment
  choice, not a worker capability or a public setup requirement.
- Workers run as a non-root numeric identity, without privileged mode, added
  capabilities, devices, host PID/IPC namespaces or host networking. Drop Linux
  capabilities and set no-new-privileges. Use a read-only image filesystem and
  explicitly approved writable task/scratch paths; fail if an image requires
  unsupported privileges rather than silently relaxing them.
- Mount only the task worktree and approved per-attempt scratch locations. Do
  not mount the host home, main checkout, provider configuration directories,
  SSH agent, or broad repository parent. Validate canonical source paths and
  mount targets before launch; reject mount escapes and overlapping mounts that
  replace security-sensitive paths. Keep SELinux confinement enabled; do not
  relabel shared host directories to make a test pass.
- A linked worktree's `.git` file points at host repository administration data.
  Git operations and artifact diffs stay host-side for the first slice. Mask
  that pointer with a read-only synthetic file in the container; do not mount
  the common Git directory or let a worker rewrite the host's pointer. Tasks
  requiring in-container Git operations are unsupported until a scoped design
  is evaluated. The first representative task edits files and runs tests; the
  host produces its patch. #50 must test this boundary explicitly.
- Default worker networking is `none`. Image resolution/pull is a separate host
  preparation step. Explicit outbound networking requires an approved profile;
  bridge networking alone is not an egress allowlist. No host ports are published
  by default. Baseline #54 trials require neither network nor service fixtures.
- No credentials are required for the synthetic first trials. Profiles may name
  references, but the first runner rejects nonempty secret requests until a
  supported provider-specific delivery method is implemented and reviewed.
  Never copy a provider's entire home/auth directory. #51 may expand controlled
  secret delivery if the evaluation demonstrates a need; deferral does not waive
  the minimum boundary checks above.
- Treat outputs as untrusted data. Keep raw logs private and size-bounded. Collect
  declared artifacts with containment, symlink and size checks; never execute
  them during collection. Public reports contain sanitized measurements, commit
  identities and artifact hashes, not transcripts, secrets or host inventories.

## Scope and next decisions

First evidence slice: **#48 → #49 → #50 → #54**.

- #48: this ADR and the [evaluation rubric](docker-worker-evaluation.md).
- #49: validate minimal internal execution profiles; default remains native.
- #50: smallest runner, minimum boundaries, ownership/recovery, artifacts and
  cleanup needed to run the trials. No automatic task dispatch.
- #54: comparable trials and explicit stop/iterate/proceed recommendation before
  further investment. A running Docker daemon or passing hello-world is only a
  prerequisite, not this decision's evidence.

#51 (expanded permissions/secret support) and #53 (richer lifecycle/heartbeat/
artifact integration) are conditional on that decision. #52 (service fixtures)
is deferred until a demonstrated task needs a disposable service. None is a
prerequisite for deciding whether the basic runner is useful.

No migration of existing sessions, default Docker requirement, public schema,
remote Docker fleet, GUI, automatic backend selection, broad provider credential
mounting, or autonomous execution is included. Completing #48 does not implement
or authorize all follow-up issues. The proposed tradeoff is deliberately limited
compatibility in return for a measurable, reviewable first experiment.
