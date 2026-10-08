# Verified upgrade release notes (opt-in)

`wb-notify` owns this optional source and its delivery state. Existing upgrader
systemd timers remain the only installation scheduler. No installer, restart,
credential import, deployment, or Todoist task creation occurs during setup or
preview. Existing attention categories and Pushover routing remain independent.

## Contract and trust

A trusted local updater compares the installed version before and after a
successful install and emits `verified_upgrade.v1` (schema_version 1): app_id
`codex_cli` or `codex_desktop`, before_version, installed_version. Identical
versions, unknown apps, unknown schema versions, extra fields, malformed or
unbounded versions are rejected. The event contains no command, arbitrary URL,
credentials, private notes, or stdout. The CLI atomically publishes it to the notifier-owned private inbox without network or delivery-lock contention.

The protected configuration and same-user filesystem boundary trust the producer.
A JSON event alone is not cryptographic installation attestation. Other same-user
programs can assert an upgrade. Do not expose this command as an unauthenticated
remote endpoint. Process exit zero is not installation verification.

The notifier looks up the fixed official GitHub release tag for CLI upgrades and
the official Codex app changelog for desktop upgrades. Desktop matching requires
the full installed version token, never a release-train substring. Official
lookup results are `matched`, `not_found`, or `lookup_failed`; not_found only means
no match in the checked source, not that notes were never published. Summaries
have at most three 250-character highlights. Entity/compatibility normalization precedes conservative filtering of URI-like tokens across all schemes and cases, relative URLs, email addresses and bare domains. Markdown/HTML link syntax and control/format characters are removed, with a small display-punctuation allowlist and a second filter after normalization. Only the separately generated official source link is retained. This can omit filenames and path/colon-bearing phrases; it is a display-text policy, not general content attestation or DLP. The Notes boundary rejects unsanitized text; an unsafe pre-fix state fails closed without rewriting immutable receipts or resending.
Missing highlights do not invalidate a matched official release. Sources and
responses are size bounded; redirects and environment proxies are disabled.

## Disabled example configuration

Merge this optional field into a private, owner-only (0600) notifier configuration.
The example contains synthetic identifiers. A real destination and explicit
persistent credential grant must be reviewed before enabling. An existing Tasks
credential or connected chat app is not implicitly granted to this local worker.

```json
{
  "enabled": false,
  "state_dir": "~/.local/state/starforge-workbench-notifier",
  "upgrade_notes": {
    "enabled": false,
    "source_id": "codex-updaters",
    "project_id": "example_notifications",
    "token_file": null
  }
}
```

The outer enabled switch governs existing attention notifications. The separate
upgrade_notes.enabled switch governs only upgrade recording and delivery. To
prepare a preview with synthetic data, explicitly enable only this source in an
isolated configuration and leave token_file null. Preview reads no token, does
not look up notes, does not create state, and cannot send.

```sh
wb-notify upgrade-record --config /private/example-notifications.json \
  --app-id codex_cli --before-version 1.2.2 --installed-version 1.2.3
wb-notify upgrade-preview --config /private/example-notifications.json
wb-notify upgrade-status --config /private/example-notifications.json
```

An explicitly granted token_file is raw token text, owned by the notifier OS user,
mode 0600, regular file, without symlinks. The worker never discovers or reuses
other credentials. Credentials are never in events, state, logs, commands, or
committed examples. A personal API token may have broader account powers than
this implementation uses; project routing does not create token-level scope.

`once` and `watch` consume pending upgrades alongside ordinary attention using the
existing notifier lifecycle. `upgrade-once` consumes only upgrades. These commands
can send when explicitly enabled; they are not preview. If watch was previously
running with attention disabled, it must be explicitly started for the new source
as part of a reviewed deployment. This change installs no service or timer.

## Durable delivery and reconciliation

`upgrades.json` is a source-specific part of the notifier's private delivery state,
sharing its single worker lock and atomic fsync writer. Producers atomically link first-wins input files in the private upgrade-inbox directory without taking the delivery lock. The worker imports under its lock, fsyncs the canonical receipt, then removes the input and fsyncs the inbox. Crash leftovers are re-imported idempotently; the inbox is not a second scheduler or task ledger. It is not a task or job
scheduler. It retains the first event and permanent receipt for each source,
app, installed version, Todoist destination. Completing or deleting a remote task,
rollback/reinstall of the same version, restart, or late notes cannot recreate it.
No routine no-change polls, session refresh results, or other notification
categories produce release-note tasks. The 10,000-receipt limit fails closed;
receipts are never silently pruned. Changing source/destination against existing
state fails closed. Review any deliberate state migration rather than deleting
receipts to reconfigure a destination.

Before sending, the worker persists the exact event, notes, stable command UUID
and temporary resource UUID, then a sending intent. It sends one Todoist Sync v1
item_add command with only project_id, content, description. No due date, labels,
priority, assignment, or account-resource query is added. A receipt requires both
sync_status[command_uuid] == "ok" and a valid mapped task ID. The API's documented
command UUID deduplication is defense in depth; this implementation does not claim
exactly-once delivery or automatically replay uncertain commands.

Only known pre-submission connection/pool failures receive bounded retries
(max_attempts and min_interval_seconds from existing notifier configuration).
Rejections block; timeouts after possible submission, malformed/lost acknowledgements,
unknown exceptions, or crashes during sending become unknown and hold this
source. Other attention sources continue. No retry runs an installer or restarts
an app. Lookup results and payload are frozen after the first delivery attempt;
a missing-notes task honestly reports its lookup status and is not duplicated
when notes become available later.

Inspect upgrade-status locally for event/command IDs. After the operator confirms
the existing task in the exact destination (including completed tasks), bind that
receipt explicitly:

```sh
wb-notify upgrade-reconcile --config /private/example-notifications.json \
  --event-id EXACT_EVENT_ID --command-id EXACT_COMMAND_UUID --task-id CONFIRMED_TASK_ID
```

This is a local operator assertion, not automatic remote verification. It sends
nothing and never retries creation. There is intentionally no blind retry button
for uncertain delivery. A known rejection likewise requires operator diagnosis;
there is no automatic unblocking or remote reconciliation search. After correcting a known rejection or exhausted confirmed-not-submitted retry, explicitly queue the same event/command with upgrade-retry --event-id EXACT_EVENT_ID --command-id EXACT_COMMAND_UUID. It preserves the original payload and UUID and refuses unknown/sending/delivered receipts.

## Updater deployment boundary

The installed personal updater directory was not a Git checkout during this
implementation. Its proposed patch is retained locally for review, not deployed.
The patch routes only successful changed installations when
WB_UPGRADE_NOTES_CONFIG and optionally WB_NOTIFY_EXECUTABLE point to explicitly
reviewed absolute paths. It records before session refresh/desktop restart, keeps
existing timers, preserves legacy behavior when unset, and suppresses the second
Pushover upgrade message when explicitly routed. Other maintenance categories
remain unchanged. It never receives the Todoist token.

A crash between installation verification and durable publication, disk-full,
missing notifier executable or invalid configuration can lose the local handoff.
The updater reports publication failure without rerunning installation. The
operator must reconcile the installed version and record the missing verified
event; the notifier cannot infer an upgrade from a later unchanged poll. This is
not an atomic transaction with npm/RPM. Do not enable the route until the exact
updater patch, worker installation, destination and credential permissions pass
review.

Protocol reference: [official Todoist Sync API](https://developer.todoist.com/api/v1/#tag/Sync/Overview).
Automated checks use synthetic notes, fake tokens and MockTransport only; they do
not prove live account access or real installation behavior.
