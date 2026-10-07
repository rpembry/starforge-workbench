# Next local Qwen integration slice (proposed)

This branch is separate from draft PR #284. Its starting point is reviewed head
`e034aea6c94f4441c0c245323c89e322ac7f3eff`; combined review and operator acceptance
remain pending. No inference or guest operation is enabled by this plan.

## Owner agreement before execution

The designated VM owner controls the one synthetic guest. This worker must not
create another guest, inspect/mount a guest disk, change the VM owner's transport,
or compete with mining or existing inference jobs. The coordinator must agree one
inference slot and resource ownership with the VM owner before synthetic inputs
are submitted. Host capacity observations are not a reservation.

## Bounded first inference proposal

- Use an already installed small local Qwen model via the existing public loopback
  Ollama endpoint. No downloads, model/settings edits, credential changes, remote
  inference, endpoint discovery scan or internal runner reuse.
- Prefer an 8B Q4 model, with approximately 5 GiB weight storage; provision an
  initial 8 GiB resident-memory budget including overhead, subject measured use.
  Larger 14B/30B models need a separate budget and are outside the first call.
- One active call, two CPU threads, CPU-only offload request, context 2048 tokens,
  output at most 256 tokens, request/response byte limits, 60-second client deadline.
  No GPU allocation is proposed. These are proposed admission/request limits,
  not proof of OS-enforced service-wide bounds.
- A client deadline alone cannot prove cancellation of inference inside a shared
  server. Verify the server's admission/abort/resource semantics read-only first;
  fail closed if existing ownership cannot enforce the agreed budget. Do not
  reconfigure or restart an existing service to satisfy this slice.

## Implementation boundary

Keep `capacity.v1` and its known synthetic inconsistency as the only job type.
The cloud still supplies no prompts, endpoints, paths, model names or commands.
The trusted local adapter constructs a fixed synthetic prompt from the registered
fixture, never proprietary VM output. Treat model text/tool requests as untrusted
data; parse a strict bounded schema with no tool execution. Return fixed failure
codes and retain detailed output locally. Request failure or crash must preserve
the existing operation/attempt identity and become uncertain without blind retry.

Do not widen the mock-only gate implicitly: introduce an explicitly reviewed
local adapter/config capability and test opt-in/disabled behavior. Use the same
human release broker and owner lock/fence. No model can access operator credentials,
approval receipts, broker storage or a release tool. Existing general report-AI,
collector and worker socket paths must not receive these outputs.

## Evidence needed

Before the single live synthetic call, agree the exact local model identity and
version, endpoint owner, resource budget and cancellation semantics. On a new
exact head, independently test no remote endpoint acceptance, bounded parsing,
fixed errors, timeout uncertainty, crash/retry identity, cancellation and no
preapproval canary disclosure. Then measure one synthetic call against the known
capacity finding and one local review; distinguish actual operator effort from
simulated approval. VM guest integration remains with its owner and requires
combined acceptance. Production/private-data readiness is not implied.
