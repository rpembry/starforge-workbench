# Optional Pushover notifications

`wb-notify` is a separate, opt-in worker. It reads `GET /api/attention` using the
existing Workbench client and sends ordinary-priority Pushover messages. It does
not run inside the API, alter actions, start agents, or change collectors. Delivery
failure cannot interrupt the dashboard or collection. If Workbench itself is
unreachable, the worker retains its state and waits for a valid snapshot; it does
not invent an outage or infer that work failed.

## Configure privately

Install the locked dependencies with `uv sync --frozen`. Create a JSON configuration
outside the checkout, owned by your user with mode **0600**. All fields have safe
defaults; absent configuration or `enabled: false` makes `once`/`watch` exit without
contacting either API, reading delivery credentials, or creating notification state.

Example placeholders (replace paths and URL in your private file):

```json
{
  "enabled": false,
  "dashboard_url": "https://dashboard.example.com",
  "api_client_file": "/path/to/private/workbench-client.json",
  "credentials_file": "/path/to/private/pushover.env",
  "state_dir": "/path/to/private/workbench-notification-state",
  "title": "Workbench",
  "categories": ["approval_needed", "collector_health", "agent_without_active_run"],
  "include_details": false,
  "poll_seconds": 30,
  "min_interval_seconds": 300,
  "max_attempts": 3
}
```

Use the existing authenticated client format described in [adapting](adapting.md).
The notification configuration requires an explicit client path, so it cannot
silently select a different deployment. The dashboard URL must use HTTPS without
credentials, a query, or fragment. Its link is sent to Pushover; it is not an access
token and does not bypass your dashboard's authentication.

The external Pushover file must also be owned by your user with mode **0600**:

```dotenv
PUSHOVER_USER_KEY=YOUR_USER_KEY
PUSHOVER_API_TOKEN=YOUR_APPLICATION_TOKEN
PUSHOVER_TITLE="Optional notification title"
```

Placeholders are not valid credentials. Actual keys must be 30 alphanumeric
characters. An optional title overrides the configured title; otherwise the default
is `Workbench`. The file accepts assignments, quoted values, unquoted titles with spaces, comments and optional
`export`; it is parsed as data, never executed or imported into the environment.
Service credential files can be used by pointing `credentials_file` to the file
provided by your service manager. Do not commit either credential file.

## Preview and explicit delivery

```sh
uv run wb-notify preview --config /path/to/private/notifications.json
uv run wb-notify preview --config /path/to/private/notifications.json --snapshot /path/to/synthetic-attention.json
uv run wb-notify test --config /path/to/private/notifications.json
```

Preview can run while disabled. It reads existing deduplication state, but does not
write it, read Pushover credentials, or contact Pushover. `preview` reads Workbench
unless a synthetic snapshot is supplied. Its pending count and payload describe the
next summary; `blocked`/`not_before` show whether real delivery would currently wait.
The title comes from the external credential file only at delivery time.

`test` without another flag previews fixed test text. **Only after deliberately
authorizing a real notification**, use `test --send` with the configuration path.
This sends one test even while normal notifications remain disabled. It reads no
attention items, retries nothing and does not alter ordinary notification state.

After deciding to enable normal delivery, change `enabled` to `true` in the private
configuration, then run `once` or `watch`:

```sh
uv run wb-notify once --config /path/to/private/notifications.json
uv run wb-notify watch --config /path/to/private/notifications.json
```

No installation command starts the worker. An optional
[example user service](../deploy/workbench-notifier.service) requires adapting its
executable and configuration paths, then deliberately installing/enabling it.
Disable notification configuration or stop that dedicated service to stop delivery;
no existing service needs restarting. `watch` rereads configuration each poll.

## Notification and recovery policy

- Each newly actionable item is identified by its stable attention ID. Changes to
  category, progress classification or reason are meaningful. Task-title changes
  count only when title inclusion is enabled. Routine evidence timestamps and poll
  order do not trigger notifications.
- Multiple pending items are coalesced into **one summary per delivery**, including
  initial startup and recovery after an outage. Default spacing is five minutes;
  new items wait behind that rate limit. Resolved pending items are removed.
- Unchanged, acknowledged items remain quiet across restarts and calendar days.
  There is no daily reminder. Observing an item's absence in a successful, fresh
  snapshot rearms it: a later outage/request can notify again. Recoveries themselves
  do not send a push. An outage and recovery entirely between polls cannot be seen.
- Snapshots older than three minutes, more than 30 seconds in the future, malformed
  or older than the last accepted snapshot cannot reset deduplication. Keep clocks
  synchronized. Duplicate identical IDs coalesce; contradictory duplicates fail
  closed rather than guessing.
- Transient connection errors, timeouts, invalid JSON and server failures retain
  pending entries. Attempts wait at least 30, then 60, then 120 seconds (or the
  configured summary spacing, whichever is longer). At most three attempts by
  default are allowed before delivery is blocked. Pending state survives restart.
- Invalid credentials, HTTP 4xx (including quota exhaustion), or a response declaring
  rejection block retries immediately. Fix configuration/quota first, then use
  `once --retry-pending` to explicitly unblock and reset the attempt budget. It
  preserves the rate limit and rechecks current attention before sending. A blocked
  worker does not silently retry because its process restarts or new items arrive.
- Receipt is recorded only after HTTP 200 **and** JSON `status: 1`. This means the
  service accepted the message, not that a person read it. Pushover offers no
  idempotency key for ordinary messages: a lost response, or a crash between remote
  acceptance and local acknowledgement, can produce a duplicate on retry. Atomic
  state, a process lock and persisted backoff reduce this risk but cannot promise
  exactly-once delivery across that boundary.

These response and retry choices follow the [Pushover Message API](https://pushover.net/api).
No response body or credential is printed on failure. CLI status reports include
counts and fixed error codes. Correct a blocked condition rather than deleting state.

## Data leaving Workbench and local state

By default, Pushover receives its authentication keys, the configured title and
link, generic category counts, and a reminder that missing visibility/progress is
not confirmed task failure. It does **not** receive task names, IDs, project names,
collector hostnames, reasons, raw records or transcripts. Workbench authentication
headers are never forwarded to the Pushover endpoint; redirects are not followed.

`include_details: true` explicitly permits up to four abbreviated task/collector
titles in a summary. No task details/body or raw transcript is read. These titles
may contain private information; preview the payload before choosing this option.
The notification itself grants no permission to execute, approve or complete work.

Use a dedicated state directory. The default is
`~/.local/state/starforge-workbench-notifier`; never point it at another app's state.
The worker creates a mode-0700 directory and mode-0600 lock/state files, storing
hashed IDs/fingerprints, retry metadata and acknowledgement timestamps, not task
text or credentials. Atomic replacement and locking protect restart/concurrent use.
Corrupt, unsafe or source-mismatched state fails closed. The worker never resets it
silently. Changing deployment/client paths requires deliberate state reconciliation.
State grows with current eligible items, not the full event history.

## Tests

`uv run pytest -q tests/test_notifications.py` uses synthetic snapshots, isolated
state and mocked HTTP delivery. Coverage includes category selection, content
privacy, restarts, heartbeat churn, recovery, meaningful changes, bounded retries,
backoff, acknowledgement checks, invalid credentials, stale/corrupt state, locking,
disabled operation and explicit test delivery. Ordinary tests never contact Pushover.
