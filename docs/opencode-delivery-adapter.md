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
Matching records are treated as chronologically ordered and only the last
terminal one for a given instruction is reported, so a transient error the
agent went on to resolve is not what gets reported.
Although the OpenCode API response includes message parts, the adapter never
logs or persists them to disk: the durable `/results` report the worker sends
carries only the state/reason pair, never message content. The #61
[synthetic spike](opencode-delivery-live-smoke.md) established these limits on
OpenCode 1.18.31. A later authorized synthetic production acceptance observed
a normal correlated assistant completion without treating it as task success.

## Ephemeral live preview

Separately from the durable state/reason pair above, `_response` also extracts
a bounded excerpt of the same terminal record: joined `text` parts for a clean
completion, or the typed error's `message` for an error, control characters
stripped, capped at 500 characters. This excerpt is held only in
`OpenCodeDelivery._previews`, an in-process dict, never written to the
attempt-marker file or anywhere else on disk. `take_preview(instruction_id)`
is the only way out: it pops and returns the excerpt once, so a second call
(or a process restart) gets nothing. `mark_response` also clears it as a
safety net, so an excerpt can never outlive the instruction it belongs to
even if the worker never calls `take_preview`.

The worker sends whatever `take_preview` returns, best-effort, to
`/api/instructions/{id}/response-preview` immediately before its `/results`
report for the same instruction. The server does not persist it either: it
authenticates the same lease token used for `/results`, then fans the excerpt
out only to operator dashboard tabs currently connected to
`/api/instructions/preview-stream` (Server-Sent Events, `text/event-stream`,
one in-memory `asyncio.Queue` per connected tab). A viewer who is not
connected at that moment never sees it; nothing is buffered for later
delivery, and a server restart clears every subscriber. The dashboard client
(`static/response-preview.js`) mirrors the same ephemerality on its side: it
keeps entries only in the page's DOM, caps the visible list, and removes each
entry after 60 seconds, all without browser storage.

`tests/test_opencode_delivery.py` uses a fake provider and synthetic text to
cover exact-session routing, busy admission, response correlation, restart/replay, ambiguous timeout,
crash after the attempt marker, missing/unavailable sessions, loopback-only
configuration, and receipt privacy. No existing OpenCode conversation was
opened or sent input for this slice. The integrated synthetic suite covers
protected claim state, kill-switch behavior, lease reconciliation, and
exact-target routing. It does not replace the separate disposable target-host
smoke required before deployment.
