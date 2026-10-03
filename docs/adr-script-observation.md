# Externally scheduled script observation, first slice (#189; collector seam #190)

## Authority and adoption

`script_observation` is a source-only opt-in wrapper for one configured argv. The
existing cron/systemd timer remains the sole dispatch and supervision owner.
The wrapper has no schedule, retry, lock, timeout, credential, AI, container,
collector, or Workbench job authority. Coordinator jobs keep their existing
`worker.v1` identity and supervisor path. A matching name or time never links an
external run to a coordinator attempt or human action.

The source-bound local adapter supplies `ScriptIdentity`, fixed argv, optional
working directory/environment and an event sink. That adapter must bind the
identity to protected configuration; an event payload is not authentication.
Each wrapper invocation gets a new opaque run ID, two ordered events (`started`
then `exited`) or one `launch_failed` event, stable event IDs, UTC occurrence
times and process-level evidence. Child stdout/stderr/stdin are inherited.
`subprocess` receives argv directly with no shell interpolation. A reporting
exception is contained and cannot alter a confirmed child exit. Exit zero is
only process-level evidence; domain result, artifacts, external effects and
human acceptance remain unknown. No log text is interpreted as progress.
The callable requires the main thread for signal forwarding. Its sink is
synchronous and must be locally bounded by an adapter before any host job uses
it; an arbitrary blocking sink could delay the child or wrapper exit. This
first slice includes no production sink or installation instructions.

The wrapper forwards TERM/INT to the tracked direct child and returns its
shell-compatible signal exit code. Daemonizing/background children are outside
this direct-child contract. A later local adapter must reconcile process-group
ownership, scheduler signal policy and stopping evidence before any real job
adopts the wrapper. A missing final event can mean lost reporting or unobserved
termination and cannot be treated as a failed or successful job.

## Versioned seams

`schemas/script-observation-v1.schema.json` describes the event shape. The
future ingest adapter must validate kind/sequence/data, bound payload sizes,
deduplicate event IDs, reject conflicting replay, enforce source identity and
record receive time independently. No such ingest path or durable outbox is
enabled here. `read_status` is a pure, redacted projection for an eventual
Workbench adapter. Its freshness reflects receive evidence; it has no schedule
inputs yet, so it makes no due/overdue claim. It does not write another ledger.

`rgb_cue` yields a semantic producer **proposal** only. RGB #3 has not yet
established its final wire version. The output is neither sent nor routed, and
has no colors, recipients, devices or Pushover credentials. A later adapter
must translate to RGB's actual accepted contract and choose exactly one
publisher. Delivery failures and retries must not rerun the script. Existing
direct Pushover behavior remains a separate per-script migration.

## Follow-on boundaries

An optional Python helper can emit bounded domain outcomes/artifact declarations
over the same observed-run identity after source-bound ingest and validation
exist. A collector (#190) can use that result contract for normalized source
observations and deterministic change rules; it needs its own validated fetch,
schedule policy and effect reconciliation. This slice supplies none of those.

An optional Docker profile should reuse the approved #174 runtime contract,
pin its image by digest, mount script and validated configuration read-only,
and keep scratch/outbox durable outside the image. Host cron/systemd may remain
the only dispatch owner when launching that profile. An optional AI hook should
request a versioned capability alias from a separately authorized broker with
per-run budgets and no ambient provider keys or local-to-cloud fallback. Neither
profile is implemented or activated by this slice.

The existing lottery workflow is only an intended pilot. A read-only private
source review found a user-systemd job that fetches two external values, then
performs a Sheet update and a separate Calendar event creation. It writes its
own local status and returns process success after the configured effects.
The two external writes are not a single transaction and no receiver-backed
idempotency key is apparent, so a replay after uncertain completion could
duplicate an effect. Its failure monitor owns the existing notification path.
No private IDs, paths, tokens, account data or source content are copied here.
Before a separate parity migration, verify the installed timer/argv, existing
locks and signal behavior, effect reconciliation, notification parity and
rollback. Synthetic checks here do not establish parity or authorize a live run.

## Evidence and compatibility

Focused synthetic tests cover an unchanged command's streams and exit status,
launch failure, reporting outage, stale read evidence and a fake RGB producer.
No scheduler, script, Calendar, Sheet, notification provider or container is
contacted. The module is not imported during ordinary Workbench startup.
