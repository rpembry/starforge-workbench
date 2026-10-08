# Combined synthetic VM policy, release and local Qwen smoke

This branch combines frozen PR284 head
`e034aea6c94f4441c0c245323c89e322ac7f3eff` with fixed PR280 head
`ad8884d80ae302095d4ad9dad4e79056e67b175a`. PR280 has scoped independent synthetic
acceptance; PR284 has independent scoped review. Neither public branch is changed,
merged or deployed by this integration. Combined acceptance needs its own review.

## Canonical cancellation and release

`SupervisorDiagnosticFence` uses existing supervisor authority and its shared
serialization lock. The supervisor validates job, attempt, incarnation, controller,
generation, lease, recovery, cancellation and execution deadline. `SyntheticVmBridge`
requires worker and broker to use this exact ownership callback and attempt
identity. Only fixed confidential VM capacity is registered through the mock
transport. Numeric VM results and synthetic source/log/screenshot canaries remain
local. No MCP resource/artifact/log path receives them.

Prepare and dispatch share the canonical fence with supervisor cancellation and
controller replacement, then the existing attempt lock. Cancellation recorded
first denies dispatch; an already-dispatching release cannot be recalled. Human
authentication and immutable approval still belong to the separate broker. No
lease, queue, retry scheduler or acceptance ledger is added. The fast mock bridge
may fence a diagnostic; never hold this global fence around long real inference.

## Owned installed-model adapter

`LocalQwen8BAdapter` accepts only the fixed synthetic fixture. Trusted local code
provides an installed runner and public Qwen3 8B Q4_K_M GGUF matching fixed size
and SHA256. There is no download, cloud-controlled model/path/prompt/endpoint,
shared Ollama request, or service/model/settings edit. The prompt contains six
synthetic configured workers and four units of capacity, with no real guest data.
Model text is strictly parsed untrusted data, never a tool or approval.

Every adapter in the composition must reuse one owner-supplied private inference
slot, admitted by the existing attempt lock. Preflight requires 24 GiB available
RAM: 8 GiB for this workload plus 16 GiB margin for existing jobs and the separately
owned guest. This is snapshot admission; unrelated processes are not reserved.

Each call creates a UUID transient user service with MemoryMax=8 GiB,
MemorySwapMax=0, CPUQuota=200%, TasksMax=64, Nice=10, RuntimeMaxSec=60 and
KillMode=control-group. Before generation, manager properties and live kernel
cgroup memory/swap/CPU limits are checked. Generation and prompt processing use
two threads, context 2048, output at most 256 tokens and one parallel slot.
Bubblewrap supplies private PID/network/mount namespaces, drops capabilities,
and exposes only read-only installed runtime/model files, system runtime, private
scratch, proc and synthetic dev. No host home, broker state, guest disks, GPU/DRM
devices or credentials are mounted. IPC uses the owned scratch Unix socket, with
response-size and total-deadline bounds. No GPU allocation is requested.

Cleanup stops only the created UUID unit and reaps its helper, with a bounded
owned-unit kill fallback. Unconfirmed cleanup denies success/evidence; the service
runtime limit bounds remaining lifetime. Shared servers, mining, guests and
unrelated jobs are untouched. No new privileges or persistent security/auth/network
settings are installed. Durable slot intent precedes launch; a crash or unconfirmed
cleanup blocks future admission across attempts. Intent/uncertain receipts require
trusted owner reconciliation; there is no automatic inference retry or reset.

## Evidence and limits

Control tests are mocks and invoke no model. They cover canonical fencing,
cancellation, identity/lease changes, local canaries, exact approved release,
concurrent admission, live-limit rejection, kernel checks, malformed/tool-injected
model results, bounded IPC and cleanup failure. Real-model evidence is separate:
installed model, synthetic input, expected capacity deficit of two, and zero
outbound content release. Elapsed time is a smoke observation, not a benchmark
or measured human review effort.

The explicit local CLI is `examples/local_qwen_smoke.py`; pass existing `--runner`,
`--model` and the designated durable private `--inference-slot`. It uses synthetic
supervisor allocation with canonical ownership probes before/after inference,
holds no global fence during the long call and prints aggregate evidence only.
It never approves or sends a report. Do not run it without coordinated resource
ownership or use a temporary slot to bypass a retained uncertain receipt.

The VM owner alone operates the one guest. This slice's VM transport remains fake;
the real-model smoke consumes no live guest result. Separate OS identities,
production human authentication, real VM transport and confidential-data integration
remain absent. Trusted owner code and same-user attacks outside namespaces are
outside this boundary. Metadata and status/timing covert channels remain review
concerns. No production-readiness or whole-host DLP claim is made.
# Stopped synthetic guest numeric composition

`synthetic_guest_capacity.consume_capacity` is an owner-local consumer for the
fixed one-CPU, 768 MiB synthetic-console registration. It copies only the exact
successful capacity envelope into a frozen numeric value. Extra logs, model
instructions, tools, paths, and targets fail closed. The required stopped/reaped
booleans are trusted VM-owner evidence, not independently authenticated lifecycle
or freshness attestations. No cloud entry point accepts this envelope.

`GuestCapacityQwenAdapter` inherits the installed-model sandbox, durable inference
slot, and resource limits. Its fixed prompt compares two synthetic planning CPU
slots with the guest's one CPU under an explicit one-logical-CPU-per-slot
assumption. CPU count does not establish production worker capacity; memory
numbers are observation metadata. The detailed result and numeric provenance
remain local. No model result grants release approval.

The serial composition test uses mocked inference and a simulated authenticated
verifier with a fake local inbox. It proves no preapproval disclosure, rejects a
model-supplied approval, and sends only immutable reviewed bytes. It does not
record actual human authorization. A real guest trial is held pending independent
acceptance of the console transport fixes; that guest must be stopped and reaped
before the one isolated model invocation. No production readiness is claimed.

The authorized live synthetic trial used an external operator harness, rather
than a cloud entry point. Its first console report inadvertently included
synthetic numeric provenance. The guest and model were cleaned up, the detailed
report stayed local, and no real endpoint received content; this original run
does not establish strict numeric nondisclosure.

The corrected console boundary is now tracked as
`starforge_workbench.diagnostic_trial_reporting`. Its `emit_status` function and
operator-only CLI emit exactly `protocol` and an allowlisted `status`, including
on malformed evidence. The CLI accepts only an owner-local regular evidence
file with mode 0600 and a 64 KiB limit, refuses symlinks/devices, and emits no
stderr, private paths, digests, reports, numeric provenance, or exception text.
The external harness calls this tracked boundary. Captured synthetic evidence
was replayed through it without another guest or model invocation, with exact
stdout and empty stderr assertions. Independent replay review is required;
this bounded regression evidence is separate from production acceptance.

The owned Unix-socket HTTP helper accepts bounded non-streaming HTTP/1.0 or
HTTP/1.1 JSON responses requiring connection close; an optional single
Content-Length must match the complete received body. Requests explicitly send
Connection: close. Keep-alive framing remains unsupported, even with a length;
failure to close within the deadline denies. The existing 16 KiB cap includes headers; the
receive deadline applies across split reads. Duplicate/invalid/mismatched
lengths, malformed headers/status, encoded bodies and every Transfer-Encoding
(including chunked or identity) are explicitly unsupported and produce the fixed
LocalModelError. Non-JSON NaN/Infinity constants also deny. Chunked decoding,
compression, streaming and general HTTP
transport are not added. JSON/body and socket failures tested here also produce
the same fixed helper error rather than server-controlled exception text.

Framing compatibility was checked only with synthetic response bytes and fake
sockets/lifecycles. Rejected chunked completion clears success evidence and runs
the existing owned stop/reap path in the fake lifecycle, without stdout/stderr
or log canaries. No current installed-server framing convention was reverified,
and no real model, guest or endpoint was invoked for this follow-up. These tests
do not establish universal error sanitization or production readiness.
