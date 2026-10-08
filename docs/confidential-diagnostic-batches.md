# Synthetic confidential diagnostic batches

This opt-in library slice addresses #281 and #282 and supplies the fake trial
for #283. It is infrastructure preparation, not a production release or a live
Qwen/VM integration. Ordinary Workbench startup is unchanged. No model downloads,
provider connections, VM boot/mount, identity provisioning, permission changes,
or private-system inspection are involved.

## Authority and local ownership

The existing coordinator store and supervisor remain the owners of jobs,
attempts, queue limits, leases, cancellation and acceptance. These libraries
operate on one owner-bound attempt; they are not a second scheduler. A trusted
local ownership callback supplies active/cancelled/unknown lifecycle evidence.
Unknown evidence denies execution. Cloud requests do not supply that callback.
The public API/server and general worker socket are deliberately not wired to
this workflow: their existing log/result/artifact channels carry free-form data
and must not be used to publish confidential diagnostic output.

The sole registered operation is `capacity.v1` against `synthetic-1`, with no
parameters. Its fixed fixture checks configured worker capacity against available
capacity. The fake Qwen adapter produces evidence; a model result is neither
execution authority nor approval. There is no shell, filesystem selector,
executable, endpoint, credential, or arbitrary tool request in the request schema.
The request carries `diagnostic.batch.v1`, job/attempt/operation identity, deadline,
and a one-step bounded output budget. Unknown fields/types/operations deny.

The owner provides an existing private attempt directory. Existing private-file,
atomic write and attempt lock primitives protect local receipts. Immutable request
identity and intent precede the fake analysis; a restarted running attempt becomes
uncertain and cannot silently rerun. Detailed fixture canaries and model reports
stay in this local state, including on failure or cancellation. Cancellation
blocks further execution and publication; it cannot undo bytes already delivered.

## Outbound contract

All automatic output is exactly `diagnostic.status.v1` plus one status enum:
`queued`, `running`, `report_ready`, `failed`, `cancelled`, or `uncertain`.
The trusted operator policy explicitly selects allowed values and the registered
fake destination `coordinator.synthetic`. No request IDs, local paths, timings,
error strings, excerpts, logs, screenshots, or extra fields may be added. This
fixed schema rejects content rather than attempting generic redaction.

Free-form bytes first enter a local review view with their destination and exact
binding. A trusted owner-injected authentication hook identifies the human
operator; the cloud and model have no approval interface or credentials. Review
binds immutable bytes/digest, destination, payload revision, job and attempt,
release operation, principal and expiry. Editing requires fresh review. Rejection,
defer, revocation, cancellation, expiry, wrong binding and replay deny dispatch.
The trusted approval authority must durably invalidate replaced or revoked signed
receipts independently of the mutable release journal and preserve invalidation
on restart. Restoring an old approved journal must not restore a revoked receipt.
No human authentication service is deployed by this milestone: tests use a fake
local verifier. A future local UI must integrate existing operator authentication
and expose review only to that role; a claimed actor or model-produced approval
must never substitute for that integration.

The broker serializes review/dispatch/cancel/revoke under the per-attempt lock,
persists dispatch intent before sending, and sends exactly approved bytes to the
approved destination. It checks the canonical owner's active evidence again before
intent. A future supervisor driver must serialize cancellation with this same
attempt lock/fence; a callback probe alone cannot eliminate an owner change after
the last observation. Broker cancellation wins if recorded before dispatch acquires
the lock; once dispatch holds the lock, cancellation cannot recall the send.
A crash or lost acknowledgement produces uncertain delivery.
Reconciliation queries the same fake endpoint using the same operation and exact
binding; it does not resend to another destination or infer delivery from a missing
receipt. Report readiness, review, approval, dispatch and delivery are separate
from task acceptance. The fake endpoint is a test sink, not a network client.

## Separate local Qwen identity and sandbox plan

A future real local Qwen process should have a distinct operator-provisioned
identity, separate from the cloud coordinator, broker and authenticated operator.
It should receive only the approved local diagnostic capability and bounded
scratch/report storage, with no broker state, human credentials, provider tokens,
cloud collectors, general worker socket or outbound network access. A local-only
model configuration must disable remote inference and telemetry. The broker must
remain outside model-writable storage, own reviewed payloads, and mediate every
permitted outbound channel. None of that OS isolation is installed here.

The in-process fake demonstrates protocol enforcement, not hostile-code sandboxing
or whole-host DLP. A real adapter needs separate process resource/time/memory
enforcement, egress policy, bounded IPC and an integration review. Same-user code,
trusted callbacks and arbitrary external collectors are outside this enforcement
boundary. Metadata and even valid-status selection/timing can form covert channels;
the status policy therefore needs operator review and cannot prove zero leakage.
Public-safe test evidence contains only synthetic data. Real source, screenshots,
full logs and confidential fixtures must not be attached to a public PR.

## Trial and acceptance

The reproducible tests exercise a fake coordinator request, local analysis, no
preapproval content delivery, one local batch review and exact approved release.
They also exercise schema/tool injection, excessive budgets, canaries, wrong
bindings, edits, expiry, revocation/replay, crashes, cancellation and acknowledgement
loss. Run the focused diagnostic tests and the repository-required full suite.

The fake fixture has a known capacity inconsistency; correctness is compared with
that expected fact. Automated test durations and simulated approval counts are
not measured human effort, real-model utility, or an efficiency gain. Operator
minutes, actual review effort and real inference quality remain unmeasured.
The decision for this milestone is **continue synthetic review**, with live pilot
readiness blocked on accepted contracts, independent QA and separately authorized
local model/isolation/transport integration. No issue is closed by this draft.
