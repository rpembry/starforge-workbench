# Mobile OpenCode control: threat review and operations

This runbook is the deployment gate for the bounded remote-instruction path.
Source checkout, package installation, and unit installation do not authorize
or enable delivery. The server gate and each host-local worker begin disabled.
Never validate this path against an existing OpenCode conversation.

## Threat-model review

The protected assets are provider conversations, instruction text, Workbench
credentials, local provider/session identities, delivery receipts, and the
integrity of delivery evidence. The trust boundaries are the operator browser
through Cloudflare Access, the public Workbench service and SQLite queue, the
collector-authenticated host worker, protected launcher/registration state,
and OpenCode's unauthenticated loopback API.

| Threat | Enforced control | Residual risk or operator duty |
| --- | --- | --- |
| Arbitrary code or terminal injection | The envelope accepts bounded plain Unicode text only. Newlines, tabs, Unicode, and shell metacharacters are one typed provider message; no shell, argv, environment, path, tmux target, or lifecycle operation is accepted. Control characters and text over 2,000 characters are rejected. | The model may act on admitted text. Review the exact target and instruction before sending. |
| Cross-session delivery | The operator selects an opaque registered session. The owning collector resolves it through private generation-bound state before and after claim; the adapter preflights and posts only the exact `ses_` ID. | Display names are not authority. Stop if local registration or binding evidence is unexpected. |
| Stale, replaced, or foreign target | Fresh collector/session evidence and ownership are required. A generation change blocks delivery; queued work is never retargeted by name. | Heartbeats prove observation, not idle state or task readiness. |
| Replay or duplicate provider work | API creation is idempotent. A private durable marker is written before the sole provider POST. Timeout, crash, unexpected response, and unresolved restart become `uncertain`, never an automatic resend. | OpenCode does not provide proven idempotency. Preserve receipts and resolve ambiguity manually. |
| Credential or transcript disclosure | Operator and collector roles are separate. Credentials and local state must be private and outside Git. Audit/history omit instruction text, lease tokens, provider output, local paths, and provider session IDs. | The operator UI and queue intentionally contain instruction text. Apply normal database and browser-access protections. |
| Public provider endpoint or interception | The adapter accepts only explicit `http://127.0.0.1:PORT`; redirects and credentialed, wildcard, remote, HTTPS, or path-bearing origins are rejected. The unit cannot configure an origin from server data. | OpenCode's local API is unauthenticated. Verify its listener is loopback-only and do not publish or proxy it. |
| Unauthorized browser submission | Only authenticated operators can view or create instructions. The form checks the configured public origin and requires exact-target confirmation; Cloudflare service credentials cannot act as a browser operator. | Cloudflare policy, allowed operator identities, and `WB_PUBLIC_ORIGIN` require deployment review. |
| Evidence overstatement | `received` means API admission or stored-message evidence; `responded` means correlated output without proving requested work success. `uncertain` is terminal. | Use provider/session output and ordinary work review to determine whether requested work completed. |

Excluded by design: xterm.js, unrestricted terminal attachment, session start,
resume, pause, continue, stop, tmux keystroke injection, automatic instruction
retry after provider contact, and remote selection of provider origin or model.

## Private configuration and credentials

1. Use a dedicated collector-role machine credential shared only by the
   OpenCode registration publisher and its worker. The claim API requires the
   same principal that owns the registration; the ordinary collector's token
   cannot be substituted. Do not use an operator token or a browser identity.
   Keep both private JSON files caller-owned and mode `0600` in a mode `0700`
   directory. Follow the [one-context rollout](mobile-control-rollout.md) for
   the registration cutover and persistent loopback API.
2. Copy the shape in `deploy/instruction-worker.example.json` to
   `~/.config/starforge-ai-workbench/instruction-worker.json`, replace every
   `EXAMPLE` or `replace-...` value, and leave `enabled` set to `false`.
   JSON paths must be absolute; shell variables and `%h` are not expanded.
3. Create `delivery_state` on persistent local storage with mode `0700`. Never
   share it between users, hosts, or workers, and never delete its JSON receipts
   to force a retry.
4. Ensure the read-only collector publishes registrations to the configured
   `registration_state` and uses the same protected launcher state. Review the
   selected manifest contexts; allowlist only the intended OpenCode context.
5. Set `provider_id`, `model_id`, and `opencode_origin` from the intended local
   OpenCode instance. The server cannot override these values.

Rotate a worker credential by stopping the worker, replacing the private
credential atomically, testing authenticated collector access without claiming,
and restarting only after registration evidence is healthy. Revoke the old
credential after the new identity is mapped to the collector role. A changed
collector principal does not inherit registrations owned by the old principal;
republish fresh registrations rather than editing ownership in SQLite.

## Preflight and activation

Keep both gates off while performing preflight. Use a new disposable OpenCode
session, isolated provider state, synthetic text, and no production credential.

1. Confirm the service environment contains
   `WB_INSTRUCTION_CLAIMS_ENABLED=0`, restart the service if its environment was
   changed, and verify a collector claim receives
   `instruction_claims_disabled` while queued records remain queued.
2. Run the worker once with its local configuration still disabled. It must
   report zero claims and must not contact OpenCode:

   ```sh
   python -m starforge_workbench.instruction_worker \
     --config "$HOME/.config/starforge-ai-workbench/instruction-worker.json" \
     --credentials-file "$HOME/.config/starforge-ai-workbench/instruction-collector.json" \
     --once
   ```

3. Verify the exact OpenCode listener rather than relying on its configuration:

   ```sh
   ss -ltn '( sport = :4098 )'
   ```

   The listening address must be `127.0.0.1`, not `0.0.0.0`, `::`, a LAN
   address, container-published port, tunnel, or reverse proxy.
4. Review the systemd unit with `systemd-analyze security` and
   `systemctl --user cat workbench-instruction-worker.service`. Confirm its
   executable, credential/config paths, read-only home policy, sole writable
   delivery-state path, and network requirements match the host. Keep the host
   `/tmp` view: exact-registration validation must read the existing tmux
   socket, so `PrivateTmp=true` would make the worker fail closed.
5. Complete the disposable acceptance checklist below. Enabling requires a
   separate deployment authorization. When authorized, enable the server claim
   gate first, then set the local worker's `enabled` value to `true` atomically.
   Start one worker for one reviewed context and watch only aggregate logs and
   the sanitized timeline.

## Kill switch and rollback

For an immediate local stop, stop the user unit. Then atomically set the local
configuration's `enabled` value to `false`; the running worker reloads it on its
next five-second cycle. To stop all hosts, set
`WB_INSTRUCTION_CLAIMS_ENABLED=0` and restart the Workbench service so the
process reloads its environment. The server switch blocks claims and renewals
but deliberately still accepts evidence from an already transmitted attempt.

Neither switch retracts input already admitted by OpenCode. Do not represent a
service stop as cancellation. If provider interruption is required, inspect the
exact disposable or production target through the normal local administrative
workflow and obtain the same authorization that direct provider control would
require.

Rollback the worker package/unit only after both gates are off and the unit is
stopped. Preserve the server database, registration state, and delivery
receipts. Code rollback must not downgrade or manually rewrite queue rows.
Queued instructions may safely expire; claimed or uncertain instructions need
the reconciliation rules below.

## Recovery and reconciliation

**Server restart:** back up and preserve the SQLite database before changing a
release. After restart, list the affected instruction IDs and verify their
history. Queued records and lease evidence are durable. Keep workers disabled
until the server kill switch is confirmed off or the intended state is reviewed.

**Worker restart:** preserve `delivery_state` and registration state. Restarting
with a prior attempt marker must perform lookup/reconciliation and must not POST
again. Missing receipts after a possible provider attempt are an incident, not
permission to retry.

**Offline host:** leave queued records untouched. They expire by server time and
must not be extended or retargeted. Restore collector heartbeat and exact local
binding evidence before allowing an unexpired record to be claimed.

**Stuck claim:** a claim lease is at most 30 seconds and never extends beyond
instruction expiry. Wait for that boundary, then read the instruction through
the API/UI to trigger and observe reconciliation. An abandoned lease becomes
`uncertain`; do not update SQLite, forge a result, return it to `queued`, or
create a replacement instruction.

**Uncertain delivery:** disable the local worker for that context and preserve
the receipt. Record the opaque instruction and registration IDs. Inspect only
the exact local OpenCode session/message identity using authorized local tools.
A matching stored user message proves receipt, not response or work completion;
a 404 does not prove the POST failed. Never delete the receipt or resend the
same intent automatically. Escalate for an explicit human decision.

**Credential or target compromise:** stop the worker, disable server claims,
revoke the credential, preserve evidence, and rotate credentials. Republish a
new opaque registration after rebuilding trusted local binding state. Old
queued work must expire against the old registration.

SSH and direct tmux attachment remain administrative fallbacks for inspection
and recovery only. They are not equivalent delivery transports. Do not paste or
inject the queued instruction through tmux, and do not use fallback access to
claim that Workbench delivery succeeded.

## Disposable phone acceptance

Automated coverage uses a 360 x 740 Chromium viewport with a synthetic server,
two disposable registration identities, and no provider. It checks navigation,
exact-target confirmation, Unicode and shell-like text as inert content, the
queued timeline, absence of lifecycle controls, no horizontal overflow, and
that only the selected registration owns the queued instruction. Separate
worker tests route the selected synthetic registration to one fake adapter and
assert that no other session receives it.

Before production enablement, repeat the UI portion from the intended Android
browser through the reviewed Cloudflare Access policy, but target only a newly
created disposable OpenCode session. Use synthetic text. Verify the other
disposable session has no instruction or provider message, the selected session
has exactly one, and the timeline says `received` rather than completed. Stop
and disable both gates after the demonstration.

The automated phone-view and fake-provider evidence is not a real Android,
Cloudflare, target-host, or real-provider result. Until the manual checklist is
recorded for the deployment, production delivery remains gated.
