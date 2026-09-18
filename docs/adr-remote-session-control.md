# ADR: bounded remote instructions for registered agent sessions

Status: proposed for review in #58. Provider-delivery details remain gated by
the disposable OpenCode spike in #61. This document defines contracts and safety
boundaries; it does not add a remote-control endpoint or authorize live delivery.

## Decision and motivation

Add remote control as two distinct capabilities: collector-owned session
registration and operator-authored plain-text instructions. The public Workbench
service stores intent and delivery evidence. A host-local worker resolves an
opaque registration to a provider session and performs delivery through a
version-validated localhost provider API. The service never receives a shell
command, tmux target, working directory, environment, argument vector, or
executable payload.

SSH and direct tmux attachment remain the administrative fallback. A browser
terminal, provider lifecycle controls, automatic session resumption, and tmux
keystroke injection are outside the first release.

The design preserves Workbench's existing evidence boundaries. Process presence,
recent terminal activity, an open permission request, instruction receipt, and
task completion are different facts. In particular, the current OpenCode
integration cannot reliably prove idle state.

## Registered session contract

A registered session is an allowlisted delivery capability reported by a local
collector. Its public identity is a random opaque ID. Provider session IDs, tmux
server/socket/session/pane names, local paths, process arguments, and credentials
remain on the host and are never returned by the public API.

The registration contains only bounded display and correlation metadata:

- opaque session ID and opaque host registration ID;
- display name, host display name, and provider kind;
- verified Workbench run/action/objective references when available;
- evidence state, reason, observation time, last activity time, and heartbeat;
- optional sanitized summary derived from structured Workbench metadata.

The first release does not derive a summary from prompts, responses, terminal
contents, reasoning, tool payloads, questions, errors, or raw transcripts.

Collector observations may assert only directly evidenced states:

| Evidence state | Meaning |
| --- | --- |
| `present` | The registered provider process/session binding was positively observed. It does not prove progress or readiness. |
| `attention_needed` | A generation-linked permission or question incident is open. |
| `provider_error` | A generation-linked typed provider error was observed. |
| `stopped` | The exact registered process generation has positive stop evidence. |
| `unknown` | Available evidence cannot establish a stronger state. |

`stale` and `offline` are server-derived visibility conditions, not collector
claims. A stale registration has exceeded the session heartbeat threshold while
its host collector is still visible. An offline registration belongs to a host
whose collector heartbeat has exceeded the host threshold. Neither condition
means the task or provider failed. `working` and `idle` are not first-release
states unless a later provider contract supplies generation-safe evidence.

The collector principal that first creates a host/session registration owns it.
Another collector principal cannot update it, claim its instructions, or reuse
its opaque identity. Observations carry an increasing sequence and timezone-aware
clock. Replays, gaps, future clocks, and identity/provenance changes fail closed.

## Instruction envelope

An operator creates an instruction against one fresh, controllable registered
session. The accepted envelope contains:

- an operator-generated idempotency key of 16–128 URL-safe characters;
- the opaque registered-session ID;
- plain Unicode instruction text of 1–2,000 characters;
- an expiry between 1 and 60 minutes, defaulting to 15 minutes.

Leading and trailing whitespace is removed. NUL and disallowed control
characters are rejected. Newlines, Unicode, and shell metacharacters are data,
not syntax, and must reach only the provider's typed message field. The server
does not parse instructions into operations. A second create with the same
principal and idempotency key returns the original record only when target, text,
and expiry match; otherwise it conflicts.

New instructions are rejected for unknown, stale, offline, stopped, or
uncontrollable registrations. An instruction already queued when its worker
becomes unavailable remains queued until its expiry. It is never silently
retargeted to a replacement process generation.

## Delivery state machine

| State | Authority and transition |
| --- | --- |
| `queued` | Created by an authenticated operator after target validation. |
| `claimed` | The owning worker leases a queued instruction until a bounded UTC time. |
| `received` | The worker has evidence that the provider accepted this logical instruction. This is not evidence of response or task completion. A later correlated output may advance this state to `responded`; provider delivery itself is not retried after `received`. |
| `responded` | Optional later evidence links subsequent provider output to this instruction after `received`. It is not evidence that requested work succeeded. |
| `failed` | A definite pre-acceptance failure makes automatic delivery retry unsafe or impossible under policy. |
| `expired` | Server time passed expiry before provider acceptance. |
| `uncertain` | Delivery may have crossed the provider boundary, but acceptance cannot be proved after an ambiguous failure. No automatic retry occurs. |

`responded`, `failed`, `expired`, and `uncertain` are terminal states. `received`
is terminal for provider delivery, but it may advance once to `responded` when
the provider contract supplies a later-output correlation; that transition does
not imply requested work succeeded. A worker may renew its own unexpired claim.
After a lease expires, the same owning worker or its replacement may reclaim
only when the provider contract proves the prior attempt did not reach the
provider, or when provider idempotency makes replay safe. Otherwise the server
moves the command to `uncertain`.

The #61 spike must determine whether OpenCode accepts a stable client message ID
and deduplicates it across reconnects. Until that is proven, the contract offers
at-most-one *attempt*, not at-most-once provider delivery. A timeout or worker
crash after transmission begins is `uncertain`; it is never automatically sent
again. Tmux keystroke injection has no provider receipt and is excluded from the
MVP.

Submitting while a provider generation is active is also gated by #61. The
worker must either use an observed provider-supported queueing contract or reject
the claim with a safe retryable reason before transmission. It must not infer
readiness from terminal appearance or elapsed time.

## API and authorization shape

Exact route spelling may change during #60/#64, but responsibilities are fixed:

- operators list/get registered sessions and their bounded delivery history;
- collectors register and heartbeat only sessions they own;
- operators create instructions and cannot claim or report delivery;
- collectors claim and report only instructions for their owned registrations;
- unauthenticated callers have no access, and collectors cannot enumerate other
  hosts, sessions, instructions, or operator work records.

Claims use a server-generated lease token returned only to the owning worker.
State changes are transactional compare-and-set operations. Instruction text is
available to the operator and intended worker but is not copied into audit-event
details, logs, notifications, status summaries, or error responses. Audit data
records actor, target opaque ID, timestamps, state, and bounded reason codes.

The service applies per-principal and per-session creation limits. Initial
implementation values belong in reviewed constants and tests, not deployment
configuration supplied by the client. Expiry and rate rejection do not echo the
instruction.

## Failure and recovery rules

- Server restart preserves queued commands, leases, terminal states, and audit
  history in server-local SQLite.
- Worker restart uses protected local receipt state when the provider contract
  requires it and reconciles before claiming another command for that session.
- Host or session replacement gets a new opaque generation identity. Old queued
  commands expire; they do not follow a display name.
- Provider unavailable before transmission returns the claim to a bounded
  retryable condition while unexpired. Definite provider rejection is `failed`.
- Connection loss after transmission begins is `uncertain` unless provider-side
  idempotency or lookup proves the outcome.
- A kill switch stops new claims and delivery without deleting pending records.
- Missing collector evidence changes visibility to stale/offline; it does not
  rewrite delivery evidence or task status.

## First implementation sequence

1. #61 validates the exact OpenCode delivery and idempotency semantics.
2. #60 adds the provider-neutral registered-session backend.
3. #59 publishes registrations without adding control behavior.
4. #64 adds the durable queue using the proven delivery guarantee.
5. #63 adds the local worker and OpenCode adapter.
6. #62 and #65 deliver the mobile read and send experiences.
7. #66 performs deployment hardening and disposable Android acceptance.

Any #61 result that cannot safely address an exact session pauses delivery work,
but does not block the read-only Sessions view. No implementation step enables a
live worker, changes a provider binding, or deploys the service without separate
authorization.
