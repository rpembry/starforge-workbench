# OpenCode attention: live validation

Validated on 2026-09-12 with OpenCode 1.18.30 and the real OpenAI
`gpt-5.6-sol` provider. This was a deliberate disposable integration test,
separate from automated fixture coverage. Existing development sessions were
not used as test subjects.

## Isolation and method

A loopback-only OpenCode server used separate temporary configuration, home,
XDG directories, session database and a private attention queue. Sharing and
MCP were disabled. Only synthetic prompts were submitted. Existing OpenAI
authentication was supplied in memory; credentials and runtime artifacts are
not included here. The bridge queue was consumed by the actual observer into
an isolated SQLite Workbench through the authenticated FastAPI TestClient.
Attention responses and server-rendered dashboard HTML were checked together.
This did not deploy a service or exercise desktop notifications/Pushover.

## Results

| Check | Result |
| --- | --- |
| Built-in question asks for a synthetic color choice | One waiting question appeared in attention and dashboard. |
| Answer the question | Its terminal tool event resolved the incident; attention became empty. |
| Next turn asks to run a harmless `printf` command | One permission wait appeared in attention and dashboard. |
| Reject that permission | `permission.replied` resolved the incident; the command was not authorized. |
| Restart observer with persisted cursor | No new submissions or restored attention. |
| Replay entire queue into the same database | Five records rejected as stale/duplicate, current generation accepted idempotently; no restored attention. |
| Content minimization | Six records across two generations contained lifecycle metadata only; no synthetic question, answer, command, output or credential text. Queue mode was 0600. |
| Action separation | No accepted actions were created throughout the test. |

The first live permission check exposed a bridge compatibility bug missed by
SDK-shaped fixtures. The actual runtime emits `permission.asked` with
`tool.messageID`, then `permission.replied` with `requestID`. The bridge had
expected `permission.updated`, a top-level `messageID`, and `permissionID`.
The corrected mapping passed the live open/reply lifecycle. Regression fixtures
now use runtime-shaped events and ignore requests lacking a linked tool identity.
The API retains legacy provenance acceptance for already queued records.

Full automated validation: 183 passed, two opt-in desktop integration tests
skipped, four subtests passed; `git diff --check` passed. Automated coverage also
checks older generations, ordering, duplicate evidence, orphan resolutions,
provider errors, missing evidence and queue recovery.

## Scope limits

Only question and permission lifecycles were verified against the real provider.
Typed assistant provider errors remain fixture-tested. Authentication and rate
limit error subtypes, child sessions and subagents were not live-tested.
Generation-less idle/status/error events remain deliberately unsupported;
missing evidence means unknown. These observations never authorize execution
or establish accepted-action completion. Installing the changed bridge and
API in an existing deployment is a separate operational step.
