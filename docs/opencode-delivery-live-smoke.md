# OpenCode delivery: live validation

Validated on 2026-09-17 with OpenCode 1.18.31 against a disposable synthetic
OpenAI-compatible mock provider. This was a deliberate disposable integration
test for the issue-61 delivery spike, separate from automated fixture
coverage. Existing development sessions were not used as test subjects.

## Isolation and method

A loopback-only OpenCode server (`127.0.0.1:4098`, `--hostname 127.0.0.1
--port 4098 --pure --print-logs`) used separate XDG configuration, data,
cache and state under `spike/oc-61/` inside this worktree; nothing else was
hooked into the environment. The configured provider (`openai`,
`baseURL http://127.0.0.1:4097/v1`) pointed at a synthetic mock
(`spike/oc-61/mock_provider.py`; deterministic completions, configurable
`MOCK_DELAY_MS`). All prompts were synthetic and clearly labelled. The server
warned `OPENCODE_SERVER_PASSWORD is not set; server is unsecured`; it was
never exposed beyond loopback (`ss` confirmed both listeners bound to
`127.0.0.1`). No existing sessions, databases or deployments were touched.
Raw sanitized probe records (method/path/status/field evidence only) live in
`spike/oc-61/evidence/` and are not committed under the default-deny ignore
policy.

## Results

| Check | Result |
| --- | --- |
| Exact-session addressing | `GET /session/{id}` → 200 echo; `GET /session/ses___nonexistent___` → 404 `NotFoundError`. After `DELETE /session/{id}` → 200, id-addressable lookups → 404. |
| Client-supplied message identity | `POST /session/{id}/message` with `messageID: msg_...` → 200; the message record is stored *under the client's id*, and the assistant message carries `parentID` equal to that client id. |
| Client-addressed message lookup | `GET /session/{id}/message/{clientID}` → 200 when admitted, 404 `Message not found` when not. |
| Acceptance acknowledgement | `POST /session/{id}/message` → 200 only after the turn completes; `POST /session/{id}/prompt_async` → 204 immediately; `POST /api/session/{id}/prompt` → 200 `SessionInputAdmitted` with `admittedSeq`. |
| Retry duplication risk | Resending the *same* `messageID` returns 200 but appends a second text part to the same message (no server-side dedup). Same text with a *new* `messageID` creates a distinct message. |
| Busy session | While a generation is in flight: `POST /api/session/{id}/wait` → 503 `ServiceUnavailableError`; `DELETE /session/{id}/message/...` → 409 `SessionBusyError`; `prompt_async` and `/api/.../prompt` (`delivery: queue`/`steer`) still admit → 204 / 200 with sequences; a synchronous `POST .../message` admits the user message but the call waits on the busy generation (no rejection, can hang). |
| Later-output correlation | `GET /session/{id}/message` returns the full graph; each assistant message links back to the client-supplied user message via `parentID`. The durable SSE `GET /api/session/{id}/event` is documented to deliver `msg_`-keyed events (`PrompAdmitted` with `admittedSeq`, step/text started/ended, step failed) but emitted no frames during the bounded capture window. |
| Loop/termination artifact | Against text-only synthetic completions the build-agent loop never emitted a finish, producing unbounded consecutive assistant turns (interrupted via `POST /api/session/{id}/interrupt` → 204). This affects how a synchronous `POST`/acceptance is interpreted, not the admission path. |

## Findings and decisions

- **Exact addressing** is available at request time for sessions and messages;
  both `ses_`/`msg_` ids are client-visible and stable under an exact id.
- **Client-originated identity** is the only idempotency affordance OpenCode
  exposes: the caller chooses `messageID` and can re-address that exact record.
  There is no server-side request dedup, so blind retry of a timed-out POST
  duplicates parts and re-runs the generation.
- **Idempotency decision (honest `uncertain`)**: no provider-enforced
  idempotency control exists in this API. The reliable client pattern is —
  on an ambiguous outcome, `GET /session/{id}/message/{clientID}`: 200 means
  accepted (never resubmit that id); 404 means not accepted (safe to submit
  once). Resending an accepted id appends rather than replays.
- **Acceptance vs output separation**: `prompt_async`/`/api prompt` give
  explicit in-time admission (204 / 200 with `admittedSeq`); the synchronous
  message POST conflates admission with completion and can block indefinitely
  on a never-finishing provider. Delivery callers should use admitted
  acknowledgement semantics plus id-addressable lookup for correlation.
- **Busy behavior**: admission endpoints remain available while busy, but
  read-mutating and generation-coupled synchronous calls are rejected (409)
  or blocked (503 / hang). The contract should prefer `delivery: queue` while
  busy and treat long synchronous POSTs as `uncertain` until looked up.
- The `docs/adr-remote-session-control.md` contract (PR #67) should adopt:
  client-chosen `messageID` + id-addressable lookup as the correlation and
  retry-control mechanism, `prompt_async`/`/api prompt` for acceptance
  semantics, `wait`/`interrupt` for busy control, and a documented bound on
  how long a synchronous submit may wait.

## Reproducible smoke procedure

1. `MOCK_DELAY_MS=4000 nohup python3 spike/oc-61/mock_provider.py &`
   (listens `127.0.0.1:4097`).
2. Start a disposable server:
   `XDG_CONFIG_HOME=spike/oc-61/config XDG_DATA_HOME=spike/oc-61/data
   XDG_STATE_HOME=spike/oc-61/state XDG_CACHE_HOME=spike/oc-61/cache nohup
   /home/rpembry/.opencode/bin/opencode serve --hostname 127.0.0.1 --port
   4098 --pure --print-logs &`.
3. Create a session (`POST /session`), then exercise:
   exact lookup (200/404), `msg_`-identified submission (200, `parentID`
   echo), id-addressable message lookup (200/404), same-id resubmit (200,
   part appended), new-id resubmit (200, new message), `prompt_async` (204).
4. Busy window: with `MOCK_DELAY_MS=30000`, submit via `prompt_async`, then
   probe `wait` (503), message delete (409), `/api/session/{id}/prompt` with
   `delivery: queue` and `steer` (200, `admittedSeq`), and a synchronous
   `message` POST (admitted but call waits).
5. Interrupt (`POST /api/session/{id}/interrupt` → 204) and delete the
   session (200); confirm id-addressable lookups return 404. Sanitized probe
   records are written by `spike/oc-61/probe.py` and reviewed before use.

## Checks

`uv run pytest -q` and `git diff --check` were run after the committed change;
environmental skips are reported at run time (0 here). Desktop integration
tests remain explicit opt-in.

## Scope limits

Only the loopback synthetic path above was exercised. Real-provider finish
semantics, multi-user auth (`OPENCODE_SERVER_PASSWORD`), TLS, the durable
event SSE replay behaviour, subagents/child sessions and permission routes
were not validated; missing evidence means unknown. These observations
never authorize execution or establish accepted-action completion.