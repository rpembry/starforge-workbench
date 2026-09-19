# OpenCode delivery adapter (partial #63)

`starforge_workbench.opencode_delivery` is a local, one-attempt provider
boundary. The opt-in [local worker](instruction-worker.md) adds collector-owned
target resolution, claim validation, and result reporting. The durable queue
and mobile send flow do not enable delivery; deployment remains gated by the
[operations runbook](mobile-control-operations.md) and a target-host disposable
smoke.

The adapter accepts an exact `ses_` session ID, an opaque instruction ID, and
bounded text from a trusted local caller. Its OpenCode origin and model identity
come from local configuration, never from the server instruction. The only
allowed origin is explicit `http://127.0.0.1:PORT`; it does not use tmux, a
shell, provider output, or remote URLs. The caller must independently verify
that the session ID belongs to the claimed registered generation. A successful
session lookup checks the exact ID before any POST.

It derives a stable `msg_` identity from the instruction ID. Before posting, it
checks that this identity is unused, then atomically writes a private durable
attempt marker containing only IDs, a hash of the text, the phase, and—when
called by the worker—the opaque server reporting lease. The
marker must live in a caller-owned `0700` directory on persistent local
storage. A repeat call with the same instruction ID but different target or
text fails closed. A second call never posts again, including after a crash,
timeout, 404 message lookup, or API error. A 200 lookup for the already
attempted message is evidence of a stored user message, not a successful
assistant response.

| Observation | Adapter result | Automatic resend |
| --- | --- | --- |
| Exact session missing before attempt | `vanished` | None attempted |
| Session/message preflight unavailable | `unavailable` | None attempted |
| Message identity already present before local attempt | `uncertain` | No |
| `prompt_async` returns 204 | `received` (API admission only) | No |
| Transport timeout/error or other status after attempt begins | `uncertain` | No |
| Restart with attempt marker and message lookup 200 | `received` (record found) | No |
| Restart with attempt marker and lookup 404/unavailable | `uncertain` | No |

This intentionally sacrifices some delivery availability to avoid duplicating
an instruction when OpenCode does not enforce client-ID idempotency. The
`received` result means admission or stored-message evidence; it does not
mean OpenCode responded, that the requested work succeeded, or that a Workbench
task is complete. For a received worker instruction, the adapter performs a
bounded local-only metadata check over the exact session's 100 most recent
records. It reports a response only for a correlated typed error or a completed
`finish: stop` assistant record; intermediate tool-call records do not count.
Although the OpenCode API response includes message parts, the adapter neither
logs nor persists them, and the server receives only the state/reason pair. The #61
[synthetic spike](opencode-delivery-live-smoke.md) established these limits on
OpenCode 1.18.31. A later authorized synthetic production acceptance observed
a normal correlated assistant completion without treating it as task success.

`tests/test_opencode_delivery.py` uses a fake provider and synthetic text to
cover exact-session routing, busy admission, response correlation, restart/replay, ambiguous timeout,
crash after the attempt marker, missing/unavailable sessions, loopback-only
configuration, and receipt privacy. No existing OpenCode conversation was
opened or sent input for this slice. The integrated synthetic suite covers
protected claim state, kill-switch behavior, lease reconciliation, and
exact-target routing. It does not replace the separate disposable target-host
smoke required before deployment.
