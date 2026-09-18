# OpenCode delivery: synthetic live validation

Validated on 2026-09-17 with OpenCode 1.18.31 and a disposable synthetic,
OpenAI-shaped mock provider. This was a deliberate integration spike,
separate from automated fixture coverage. Existing development sessions were
not opened, resumed, or sent input.

## Isolation and method

A loopback-only OpenCode server (`127.0.0.1:4098`, `--hostname 127.0.0.1
--port 4098 --pure --print-logs`) used separate XDG configuration, data,
cache, and state directories inside this worktree. The configured provider
pointed to a synthetic mock on `127.0.0.1:4097`. All prompts and identifiers
were synthetic. The unsecured test server was never exposed beyond loopback;
`ss` confirmed both listeners were bound to `127.0.0.1`.

The original run used ignored worktree-local scripts and retained raw synthetic
responses only in ignored local state. The committed
[`deploy/oc-delivery-smoke.py`](../deploy/oc-delivery-smoke.py) reconstructs the
loopback mock, writes a fresh isolated configuration, and emits sanitized
structural probe results. Its probe output never includes prompt text,
assistant text, error messages, response bodies, message metadata paths,
headers, or credentials.
The full experiment sequence was not rerun merely to replace the helper, so
the evidence below remains the 2026-09-17 observation against version 1.18.31.

## Results

| Check | Observed result |
| --- | --- |
| Exact-session addressing | `GET /session/{id}` returned 200 with the same synthetic ID; a nonexistent ID returned 404 `NotFoundError`. After `DELETE /session/{id}` returned 200, ID-addressable lookups returned 404. |
| Client-supplied message identity | With `noReply: true`, `POST /session/{id}/message` using a client-selected `messageID` returned 200 and stored the user message under that ID. A generated assistant record, when present, linked to the client ID through `parentID`. |
| Client-addressed message lookup | `GET /session/{id}/message/{clientID}` returned 200 for a stored message and 404 `Message not found` for an unused ID. |
| API status versus assistant outcome | An early synchronous message request returned HTTP 200 **with an assistant `info.error`** (`APIError`, synthetic upstream 404). That was API completion, not a successful provider response. Separate later records demonstrated `parentID` correlation and text-part creation, but the synthetic provider did not produce a bounded successful end-to-end turn. |
| Acceptance acknowledgement | Synchronous `POST /session/{id}/message` did not return until the attempted turn ended. `POST /session/{id}/prompt_async` returned 204 immediately. `POST /api/session/{id}/prompt` returned 200 `SessionInputAdmitted` with `admittedSeq`. These statuses establish API admission/completion only, not successful assistant output. |
| Retry duplication risk | Resending the same accepted `messageID` returned 200 but appended a second text part to the same message. The server did not deduplicate it. The same text under a new `messageID` created a distinct message. |
| Busy session | During an in-flight generation, `POST /api/session/{id}/wait` returned 503 `ServiceUnavailableError`; message deletion returned 409 `SessionBusyError`; `prompt_async` and `/api/.../prompt` with `delivery: queue` or `steer` still admitted input; a synchronous message request timed out while the user record was present. |
| Later-output correlation | Message records linked assistant records to the client-supplied user ID through `parentID`. A bounded subscription to `GET /api/session/{id}/event` produced no frames, so durable SSE correlation and replay remain unverified. |
| Loop/termination artifact | The narrow synthetic Responses API implementation did not give OpenCode the finish behavior it expected. OpenCode produced consecutive assistant records until `POST /api/session/{id}/interrupt` returned 204. This prevents treating synchronous HTTP completion as evidence of a normal provider turn. |

## Findings and decisions

The local one-attempt adapter built from this evidence is documented in
[OpenCode delivery adapter](opencode-delivery-adapter.md). It is not yet a
polling worker or a deployed control path.

- **Exact addressing** is available for sessions and stored messages. Both
  `ses_` and `msg_` identities are client-visible.
- **No provider-enforced idempotency was observed.** A client-selected
  `messageID` provides correlation, but resubmitting an accepted ID mutates the
  stored message and can repeat work.
- **Ambiguous delivery remains `uncertain`.** After a timeout, a 200 lookup is
  evidence that a record with the client ID exists and therefore must not be
  resubmitted. A 404 does **not** make retry safe: the original request could
  still be in flight, not yet visible, or accepted just after the lookup. This
  spike did not establish a grace period or terminal negative acknowledgement
  that converts that ambiguity into safe retry.
- **HTTP and assistant outcomes are separate.** A synchronous HTTP 200 can
  contain an assistant record with `info.error`. A delivery adapter must inspect
  the returned or subsequently addressed assistant record and distinguish
  `error`, `completed_without_error`, and not-yet-completed. Even
  `completed_without_error` is only provider-turn evidence; it does not prove
  the requested work succeeded or completed.
- **Busy admission is observable but not exactly-once.** Queue/steer admission
  remains available while busy, but a lost acknowledgement still has the same
  uncertainty. The surrounding contract must not convert admission into
  delivered output or blindly retry it.
- The provider-neutral contract should therefore retain an explicit
  `uncertain` state. No tmux fallback is equivalent to provider admission, and
  this spike does not justify a remote-control implementation by itself.

## Reproducible smoke procedure

Run this only against a fresh disposable OpenCode server. Do not target an
existing server, session, data directory, or provider credential.

1. Confirm the binary under test and create private disposable state from this
   checkout:

   ```sh
   opencode --version
   umask 077
   export SMOKE_ROOT="$(mktemp -d -p "$PWD" .oc-delivery-smoke.XXXXXX)"
   mkdir "$SMOKE_ROOT/data" "$SMOKE_ROOT/state" "$SMOKE_ROOT/cache"
   python3 deploy/oc-delivery-smoke.py write-config "$SMOKE_ROOT/config" 4097
   printf 'Use this SMOKE_ROOT in each terminal: %s\n' "$SMOKE_ROOT"
   ```

2. In a second terminal, start the synthetic provider in the foreground. A
   30-second delay creates a bounded busy window:

   ```sh
   python3 deploy/oc-delivery-smoke.py mock 4097 30000
   ```

3. In a third terminal, set `SMOKE_ROOT` to the exact absolute path printed in
   step 1, then start OpenCode in the foreground with only the disposable XDG
   roots:

   ```sh
   export SMOKE_ROOT=/absolute/path/printed-in-step-1
   env \
     XDG_CONFIG_HOME="$SMOKE_ROOT/config" \
     XDG_DATA_HOME="$SMOKE_ROOT/data" \
     XDG_STATE_HOME="$SMOKE_ROOT/state" \
     XDG_CACHE_HOME="$SMOKE_ROOT/cache" \
     opencode serve --hostname 127.0.0.1 --port 4098 --pure --print-logs
   ```

4. In the first terminal, confirm both listeners are loopback-only, then create
   synthetic request files. These files contain no real prompt or provider
   data:

   ```sh
   ss -ltn '( sport = :4097 or sport = :4098 )'
   printf '%s\n' '{"title":"synthetic-delivery-smoke"}' >"$SMOKE_ROOT/session.json"
   printf '%s\n' '{"messageID":"msg_spike_repeat","noReply":true,"model":{"providerID":"openai","modelID":"gpt-6-astra"},"parts":[{"type":"text","text":"SYNTHETIC IDEMPOTENCY PROBE"}]}' >"$SMOKE_ROOT/no-reply.json"
   printf '%s\n' '{"messageID":"msg_spike_busy","model":{"providerID":"openai","modelID":"gpt-6-astra"},"parts":[{"type":"text","text":"SYNTHETIC BUSY PROBE"}]}' >"$SMOKE_ROOT/busy.json"
   printf '%s\n' '{"id":"msg_spike_queue","prompt":{"text":"SYNTHETIC QUEUE PROBE"},"delivery":"queue"}' >"$SMOKE_ROOT/queue.json"
   ```

5. Create the session and copy its synthetic `ses_` ID from the sanitized
   output into `SESSION_ID`:

   ```sh
   python3 deploy/oc-delivery-smoke.py probe post /session "$SMOKE_ROOT/session.json"
   export SESSION_ID=ses_REPLACE_WITH_SYNTHETIC_ID
   python3 deploy/oc-delivery-smoke.py probe get "/session/$SESSION_ID"
   python3 deploy/oc-delivery-smoke.py probe get /session/ses___nonexistent___
   ```

6. Demonstrate client identity and lack of deduplication without invoking the
   provider. The first exact lookup should summarize one text part; after the
   same-ID resubmit it should summarize two:

   ```sh
   python3 deploy/oc-delivery-smoke.py probe post "/session/$SESSION_ID/message" "$SMOKE_ROOT/no-reply.json"
   python3 deploy/oc-delivery-smoke.py probe get "/session/$SESSION_ID/message/msg_spike_repeat"
   python3 deploy/oc-delivery-smoke.py probe post "/session/$SESSION_ID/message" "$SMOKE_ROOT/no-reply.json"
   python3 deploy/oc-delivery-smoke.py probe get "/session/$SESSION_ID/message/msg_spike_repeat"
   python3 deploy/oc-delivery-smoke.py probe get "/session/$SESSION_ID/message/msg_spike_missing"
   ```

7. Exercise admission while busy. Run the first command, then issue the others
   during the mock delay:

   ```sh
   python3 deploy/oc-delivery-smoke.py probe post "/session/$SESSION_ID/prompt_async" "$SMOKE_ROOT/busy.json"
   python3 deploy/oc-delivery-smoke.py probe post-empty "/api/session/$SESSION_ID/wait"
   python3 deploy/oc-delivery-smoke.py probe post "/api/session/$SESSION_ID/prompt" "$SMOKE_ROOT/queue.json"
   PROBE_TIMEOUT=2 python3 deploy/oc-delivery-smoke.py probe post "/session/$SESSION_ID/message" "$SMOKE_ROOT/busy.json"
   python3 deploy/oc-delivery-smoke.py probe get "/session/$SESSION_ID/message/msg_spike_busy"
   ```

   A 200 lookup after the timeout means do not resubmit. A 404 leaves delivery
   uncertain and is not permission to retry.

8. Interrupt and delete only the disposable synthetic session, stop both
   foreground servers, and remove the disposable directory after reviewing its
   exact path:

   ```sh
   python3 deploy/oc-delivery-smoke.py probe post-empty "/api/session/$SESSION_ID/interrupt"
   python3 deploy/oc-delivery-smoke.py probe delete "/session/$SESSION_ID"
   python3 deploy/oc-delivery-smoke.py probe get "/session/$SESSION_ID"
   ```

The helper refuses non-loopback origins and redirects, refuses to overwrite its
generated configuration or evidence output, bounds response/event collection,
and emits structural summaries only. The disposable OpenCode server still has
no authentication; loopback binding and isolated state are mandatory.

## Checks

`uv run pytest -q`: 260 passed, 6 skipped, 4 subtests passed. The skips are
environmental/opt-in checks. `git diff --check` passed. Desktop integration
tests remain explicit opt-in.

## Scope limits

Only the loopback synthetic path above was exercised. A real provider's normal
finish semantics, authentication, TLS, durable SSE replay, subagents/child
sessions, permission routes, and a safe negative-acknowledgement retry window
were not validated. The committed helper was reviewed and locally checked, but
the complete 2026-09-17 experiment was not rerun from it. Missing evidence means
unknown. These observations never authorize execution or establish accepted
action completion.
