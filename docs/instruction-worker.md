# Opt-in local OpenCode instruction worker

Issue #63 adds a local worker that reads only collector-owned registered
OpenCode sessions, claims a server instruction for one exact registration, and
submits bounded text through OpenCode's loopback API. It never accepts a
server-supplied shell command, executable, tmux target, directory, provider
origin, model, or environment. SSH and tmux remain the administrative fallback,
not a delivery adapter.

The worker is **off by default** in two places: its private local configuration
must set `enabled: true`, and the server-side #64 environment switch
`WB_INSTRUCTION_CLAIMS_ENABLED=1` must be explicitly enabled. Installing code or a
unit does not enable either gate. Do not enable the worker before the #64 API,
the #59 publisher, the #61-version disposable smoke, and this worker are
reviewed together. No existing OpenCode conversation should be used for that
smoke.

## Local configuration

Create an owned `0600` JSON file outside Git at the path selected by
`--config`. Its parent and the `delivery_state` directory should be owned by
the worker user with mode `0700`. This is a synthetic shape, not a deployment
configuration to copy unchanged:

```json
{
  "enabled": false,
  "manifest": "/private/path/to/workbench.yaml",
  "registration_state": "/private/path/to/registrations.json",
  "launcher_state": "/private/path/to/launcher-state",
  "delivery_state": "/private/path/to/delivery-state",
  "opencode_origin": "http://127.0.0.1:4098",
  "provider_id": "openai",
  "model_id": "synthetic-model",
  "contexts": ["synthetic-opencode"]
}
```

The worker uses a separate protected collector client file with
`--credentials-file`. The OpenCode-only registration publisher uses the same
collector principal so the worker can claim its registration; the ordinary
multi-context collector keeps its existing identity. See the
[one-context rollout](mobile-control-rollout.md) for the cutover. The worker
reloads its configuration every five seconds.
Setting `enabled` to false stops future claims without deleting server pending
records or private attempt receipts. The server kill switch independently
rejects new claims. Neither switch cancels an already transmitted instruction.
Only the local administrator may choose the OpenCode origin, model identity,
context allowlist, or filesystem paths. The model identity must match the
intended local session; the server does not choose it.

## Exact target and lifecycle

Before claiming, the worker reads the collector's protected registration
state, checks an enabled allowlisted OpenCode context, verifies a live provider
process through a fresh collector-style scan, and compares the current exact
launcher binding/process generation with the published opaque registration.
It repeats that check after claiming. Missing or changed evidence prevents
provider input. A claim is renewed immediately before attempting delivery;
without confirmed renewal the worker sends nothing.

The provider adapter has one durable-attempt marker per instruction, written
before any POST. For a claimed instruction it also retains the opaque instruction
ID and reporting lease in that private marker until later response evidence is
reported or found unreportable. `prompt_async` HTTP 204 means admission only and maps to
`received`; a definite missing session before POST maps to `failed`; a provider
preflight outage maps to the server's retryable queue state. Once a POST may
have begun, timeout, unexpected status, crash, or later message lookup 404
maps to `uncertain` without automatic resubmission. Message lookup 200 after a
crash can establish a stored user message.

After `received`, each worker sweep asks the same loopback API for at most the
100 most recent records in the exact session. The response body is bounded to
8 MiB, remains only in local process memory, and is discarded after inspecting
allowlisted metadata. A completed assistant record with `finish: stop` and the
delivered user-message ID as its parent advances the instruction to `responded`.
Intermediate `tool-calls` records do not. A correlated typed assistant error
also advances to `responded` with an error reason. Neither outcome means the
requested work succeeded or completed. A restart reuses the private marker and
never sends the instruction again. The worker never logs text, lease tokens,
provider output, or private paths; only aggregate counters or a generic
unavailable marker.

The local unit recipe is
[`deploy/workbench-instruction-worker.service`](../deploy/workbench-instruction-worker.service).
Its matching disabled configuration shape is
[`deploy/instruction-worker.example.json`](../deploy/instruction-worker.example.json).
It is not installed or enabled by source checkout. The private configuration,
collector credentials, local runtime path, and server setting must be reviewed
on the actual host before any service action. After a worker crash, preserve
delivery receipts and reconcile server state before touching the queue; never
delete receipts merely to force a retry. Recovery is inspection and explicit
operator action, not tmux injection.

Follow the [operations runbook](mobile-control-operations.md) for activation,
kill-switch, restart, stuck-claim, uncertain-delivery, and rollback procedures.

## Validation boundary

Ordinary tests use synthetic registration state, a synthetic #64 server queue,
and a fake provider. They prove claim, exact-session routing, one-attempt
decisions, and result reporting but cannot prove a live collector registration
or live server/worker deployment. On installed OpenCode 1.18.31, a separate
loopback-only, disposable `opencode serve` session admitted an adapter
`prompt_async` request with HTTP 204; lookup of the exact synthetic
`messageID` returned its user record. With OpenCode's recognized
`openai/gpt-6-astra` model routed to a local mock provider, a second new
disposable session also produced a correlated assistant record marked
completed without error. This proves a synthetic normal response, not that
the requested work was done. No existing session was used. Before
enabling this worker, repeat an end-to-end disposable smoke with the exact
host-local model and registration to verify admission and result reporting.
A production acceptance on OpenCode 1.18.31 later established exact-session
API admission and a correlated completed assistant record using one explicitly
authorized synthetic instruction. That observation did not expose response
content and did not prove the requested work succeeded. The automated suite
now covers response correlation and durable replay; provider upgrades still
require the disposable smoke procedure.
