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
