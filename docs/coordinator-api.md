# Local coordinator API and CLI (#175)

`coordinator.api.create_app(store, adapter=...)` is the v1 control boundary. The
standalone `coord-server` launcher accepts an existing private state directory,
a private JSON `Policy` file, and an unused socket path inside an existing
owner-only directory. It binds only a Unix socket with mode `0600`. Run it as the
owner; keep this socket out of proxy configurations. Without an explicit
`--supervisor-socket` (the separate service's protected `control.sock`), jobs
remain durably queued. With that socket and approved runtime configuration,
the coordinator renews a fenced controller lease and dispatches committed
commands. Missing supervision never falls back to native execution.

```sh
coord-supervisor serve --state-root /private/supervisor --config /private/supervisor.json
coord-server --state-root /private/state --socket /private/run/coord.sock --policy /private/policy.json --supervisor-socket /private/supervisor/control.sock
coord --socket /private/run/coord.sock --json health
coord --socket /private/run/coord.sock --json submit job.json --key submit-2026-01
```

Both server and client use fd-relative Unix socket addresses so an owner-private
runtime directory can have a pathname longer than the Linux socket address
limit. The directory descriptor stays open for each client connection lifetime.
The protected socket is the local authentication boundary. Browser `Origin` and
cross-site requests and nonlocal Host values are rejected. Do not expose the
ASGI app through a TCP listener without a separate origin-bound authentication
adapter. API request bodies over 128 KiB are rejected while streaming, even with a missing
or false `Content-Length`; `JobSpec` bounds the
payload to 64 KiB and accepts registered policy keys, not host paths.

## Routes

| Method | Route | Meaning |
| --- | --- | --- |
| GET | `/v1/health`, `/v1/capabilities`, `/v1/capacity` | Separate availability, capabilities, configured budget and bounded reservation snapshot (unknown once 1000 jobs are reached) |
| POST | `/v1/jobs/validate` | Validate an immutable registered job spec and show budget |
| POST, GET | `/v1/jobs` | Submit with idempotency key; bounded redacted list |
| GET | `/v1/jobs/{id}`, `/v1/jobs/{id}/attempts` | Detailed job, immutable attempts and evidence |
| GET | `/v1/jobs/{id}/events` | Replay after `after_seq` with snapshot, floor, gap, next cursor |
| POST | `/v1/jobs/{id}/cancel`, `/retry` | Expected version and idempotency key required |
| GET, POST | `/v1/jobs/{id}/recovery`, `/reattach` | Read-only recovery status; durable same-attempt reattach request |
| GET | `/v1/jobs/{id}/attempts/{attempt_id}/logs` | Post-stop bounded log bytes with cursor and possible prefix-gap flag |
| GET | `/v1/jobs/{id}/attempts/{attempt_id}/artifacts[/{artifact_id}]` | Manifest and contained artifact from adapter |

FastAPI publishes `/openapi.json`. Mutations return the job and an operation key
and state: `pending`, `confirmed`, or adapter-reported `unknown`. A missing
supervisor is reported as unavailable, never as execution success. No
checkpoint-resume route exists in v1. An emergency owner stop must use the
supervisor's separate protected owner API; normal `coord` never opens its
journal or operates Docker.

After an uncertain response, repeat the **same payload, expected version, and
idempotency key**. Never mint a second key to guess whether a submission or
retry happened. `recover` only inspects. `reattach` records a durable same-attempt command;
the dispatcher verifies the exact owned runtime under its current controller
fence. Same-key replay remains safe after a lost response, while a stale new
key conflicts. `retry` creates a fresh attempt
only after positive stopped evidence. A checkpoint resume, if ever supported,
requires a separate capability and is not equivalent to either action.

`coord --json` prints one JSON object per response. `events` and `logs` make one
bounded read. `watch` and `follow` poll using the returned cursor until
Ctrl-C; detaching does not cancel. Explicit `cancel` is required to stop work.
Exit codes: `0` success, `2` input/configuration, `4` not found, `5` conflict,
`6` unavailable, `7` other HTTP error, `130` interrupted watch. Detailed reads
are available only through the protected socket. Operators should avoid
including private payloads or logs in public reports.

## Supervisor relay seam

`coordinator.dispatch.Dispatcher` relays durable `pending_commands()` through
the supervisor's owner-only `control.sock`, using the store operation ID as the
supervisor idempotency identity. It verifies the exact job, attempt,
incarnation, and launch plan before applying observations. A lost launch
response reconciles the same attempt; it does not allocate another worker.
The relay retains unknown visibility when supervision is unavailable. The
optional `--supervisor-socket` enables one coordinator controller and renews
its lease; the supervisor independently enforces deadlines and owner stop.

A cancellation committed before supervisor launch now uses the canonical launch
operation ID to create a durable no-start tombstone. Delayed launch replay
cannot start a worker after that tombstone. A stopped noncancelled attempt is
`finalizing` until `collect` returns verified process and worker evidence;
`result_ok=None` remains unconfirmed. Artifact reads use hash-verified bounded
chunks from the supervisor. `logs` reads the exported `output.txt` after stop;
its source is a bounded last-1000-lines window, so the response advertises a
possible earlier gap. Live log streaming is not yet supported. Neither API nor
CLI reads Docker or the supervisor journal directly.
