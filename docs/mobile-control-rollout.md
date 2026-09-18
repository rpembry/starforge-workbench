# One-context OpenCode rollout

This is the host-specific handoff after the disposable acceptance in #66. It
does not enable delivery by itself. Keep the server claim gate and worker
configuration off until every preflight is clean. Do not send a deployment test
instruction to an existing conversation.

## Identity and registration cutover

The instruction claim API requires the *same collector principal* that owns the
registered session. A separate worker token cannot claim a registration
published by the ordinary collector. Give the OpenCode-only publisher and its
worker one dedicated collector-role Cloudflare Access service token; leave the
ordinary collector on its existing token for all other contexts. Store the new
client configuration as caller-owned `0600`
`~/.config/starforge-ai-workbench/instruction-collector.json`, in a `0700`
directory, using the same JSON shape as the existing collector client. Never
commit or print its `client_secret`.

1. Verify the exact Cloudflare account, AI Workbench Access application, and
   current service-token policy. Create one service token with a bounded
   expiration and add **only that token** to the existing Workbench
   service-token policy; preserve the browser policy and prior token entries.
   Map its verified `client_id` (`common_name` in the signed Access JWT) to
   `collector` in the protected server Access configuration. Back up that
   configuration first. Do not put token material in the server file.
2. Test authenticated *read-only* collector access using the new local client.
   A role or Access mismatch blocks rollout. Do not infer authorization from a
   successful Cloudflare token creation alone.
3. Install the reviewed
   [`workbench-opencode-publisher.service`](../deploy/workbench-opencode-publisher.service)
   disabled. Its single `--context opencode` and protected
   `instruction-registrations.json` must match the worker's exact manifest,
   `registration_state`, `launcher_state`, and context allowlist. The publisher
   owns the registration; the worker only reads it. Both use the dedicated
   client configuration. Never copy an old registration state to the new
   publisher: it needs a new opaque ID under the new principal.
4. Review the currently installed ordinary collector's context list. Apply
   [`workbench-collector-without-opencode.conf.example`](../deploy/workbench-collector-without-opencode.conf.example)
   only after adapting it to preserve *all* unrelated contexts. Verify the
   installed `ExecStart` before and after restarting that collector. The old
   OpenCode registration may remain offline in history; it must not continue
   receiving fresh heartbeats. Starting both publishers for the same context
   would create ambiguous duplicate targets in the phone UI.
5. Start the dedicated publisher and check that exactly one *new* OpenCode
   registration is fresh, names the intended host/context, and is owned by the
   dedicated principal. Verify the exact local launcher binding without
   changing it. Leave claims off during this entire cutover; a disabled-gate
   503 does not prove registration ownership.

## Provider API and worker preflight

Install [`workbench-opencode-api.service`](../deploy/workbench-opencode-api.service)
but do not enable it before checking the installed OpenCode executable and
version. This unit uses `--pure` to omit external plugins and binds only
`127.0.0.1:4098`; it does not attach to or prompt a conversation. Start it,
check `ss -ltn '( sport = :4098 )'` for **only** `127.0.0.1`, and check the
loopback `/session/<exact-synthetic-id>` API with a new disposable session.
Never publish or tunnel the API. The API has no independent authentication on
loopback, so local account access is part of the trust boundary.

Install the reviewed
[`workbench-instruction-worker.service`](../deploy/workbench-instruction-worker.service)
disabled. Its client path must be the dedicated `instruction-collector.json`.
Create a persistent caller-owned `0700` delivery-state directory and a private
`0600` worker JSON based on
[`instruction-worker.example.json`](../deploy/instruction-worker.example.json),
still `enabled:false`. It must select only `opencode`, use the dedicated
publisher's registration state, and specify the reviewed local model. Never
delete or share delivery receipts. Run a disabled `--once` cycle and require
zero claims. Verify an authenticated claim returns
`instruction_claims_disabled`, and the pending queue contains no unexpected
work. Ownership is covered by synthetic tests; on the live host, check a
wrong-principal claim only after the claim gate opens and before starting the
worker, and require an ownership denial without any queued instruction.

## Activation and rollback

After the preflight and rollout decision, set the server claim gate first,
restart the server, verify health, and require an ownership denial for a claim
using the ordinary collector identity against the new registration. Atomically
change only the worker JSON's
`enabled` field to `true`; start the worker without enabling it at boot for an
initial bounded observation window. Require one eligible exact registration,
zero ambiguous claims, and no provider POST in the absence of a newly queued
instruction. Inspect only aggregate logs, opaque IDs, and the sanitized
timeline. Do not equate process presence or provider acceptance with completed
work. Enable the API, publisher, and worker at boot only after that window is
clean and explicitly approved.

To stop: stop the worker, atomically restore `enabled:false`, disable the server
claim gate and restart the server, then verify a claim receives
`instruction_claims_disabled`. Preserve receipts, registration history, and
queued records. Stop the publisher/API only after the worker is off. Restore the
ordinary collector's original context list if rolling back the split and
verify the old registration is fresh again; never rebind a provider session or
manually edit SQLite to force this. Follow
[the operations runbook](mobile-control-operations.md) for uncertain claims and
recovery. SSH/tmux remains the administrative fallback, never a Workbench
delivery substitute.
