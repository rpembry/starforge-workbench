# Opt-in local script observation prototype (#189)

This source-only adapter extends [the external-script observation contract](adr-script-observation.md).
It adds a private local event buffer and a redacted status command. It does not
install a service, alter a timer, enroll a real job, contact RGB or Pushover,
or write to Workbench's coordinator ledger. Existing cron/systemd remains the
only scheduler. This prototype's status is local; the ordinary Workbench API
and PWA do not ingest it yet.

## Protected descriptor and commands

`wb-script-observe run --config PRIVATE_FILE` runs one fixed argv. The command
inherits stdin, stdout, stderr and environment; an optional absolute working
directory is applied. It never uses a shell, timeout, execution retry or new
lock. The private descriptor is an owner-held, nonsymlink regular file with
mode `0600`. It has version `1`, `identity` (`script_id`, `revision`,
`adapter_id`), `argv`, `spool_dir`, and nullable `cwd` fields. The descriptor
and configured event directory stay outside Git. The event directory must
already exist, be owned by the caller, have mode `0700`, and contain no
unexpected entries. No command or path value appears in status output.

`wb-script-observe status --config PRIVATE_FILE` reads validated source-bound
events and prints redacted JSON. It does not initialize or change an empty
buffer. Its output separates observed process state, receive freshness, domain
outcome and human acceptance. Domain and human values remain `unknown` here.
An unsafe or corrupt buffer, wrong source binding, missing terminal predecessor,
or unavailable directory returns `unknown` with `reporting: unavailable`.

## Durability and bounds

The local writer uses a private lock, an owner-only temporary regular file,
file fsync, an atomic hard link to the immutable event name, and directory
fsync. Exact event replay returns without a second record; conflicting event
identity is rejected. Each event is at most 4 KiB; the directory holds at most
128 events and 512 KiB. Capacity exhaustion retains prior evidence and rejects
new writes; it never reruns the script. There is no automatic pruning,
acknowledgement, delivery transport or restart retry. A crash with a leftover
temporary file makes status unknown until the buffer is reconciled by a later
reviewed maintenance path. Missing final evidence stays unknown after the
freshness window. Lock acquisition is nonblocking; contention yields unknown
status or a rejected write, without delaying the child.

At the record limit, or with less than one maximum-size event of byte capacity
remaining, status is `unknown` with `reporting: full`. A new start might have
been rejected, so an older successful run must not remain the apparent current
result. Recovery requires a separately reviewed retention step that preserves
old records outside the active buffer, followed by new run evidence. The
synthetic test archives old records before admitting a new final event; the
CLI does not prune or archive real observations automatically.

The wrapper allows at most 100 ms per publication call. A late or failed
publication cannot change the child exit code. Its daemon thread may be lost
when the wrapper exits, so this prototype does **not** guarantee a durable
final event during disk stalls, full storage or a crash. An actual host-job
retrofit needs a reviewed bounded persistence and recovery policy, confirmed
signal/child behavior, and a single canonical Workbench ingest path. The
private spool is a limited observation buffer, not an execution ledger.

## Synthetic acceptance and follow-on

`tests/test_script_observation_local.py` runs an inert Python child against a
temporary protected directory and checks command-to-event-to-status behavior,
exact replay, mismatched source, corrupt/gapped evidence, bounds, permission
denial and a read-only empty status check. It uses no configured personal job.

A future Workbench adapter may call `status_from_spool(directory, identity,
now=..., freshness_seconds=...)` only after binding `identity` from protected
registration. It must transport validated observations to the existing
Workbench store and shared read model before exposing them through API, MCP or
PWA. The status page must not read arbitrary client-supplied paths or identify
a human task as completed from process exit. RGB translation still requires its
final producer contract and one configured publisher.
