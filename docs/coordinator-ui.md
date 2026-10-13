# Opt-in local coordinator view

The coordinator view reuses Workbench's browser style and the versioned
coordinator client. It is **not** mounted by the normal Workbench server. A
remotely deployed Workbench, or a bearer caller of that server, has no
coordinator routes. The source-only `coord-ui` launcher is a separate, explicit
local companion; do not add it to an installed service or proxy configuration
without a separate security and operations review.

The launcher accepts an existing owner-owned `0600` coordinator Unix socket,
an unused local port, and an optional loopback Workbench dashboard origin. It
binds only `127.0.0.1` and prints a one-use activation URL at a fresh
`coordinator-ui-<random>.localhost` hostname. Startup checks that the hostname
resolves only to loopback. The host-only browser cookie is scoped to this
per-process hostname, so opening another service on `127.0.0.1` or
`localhost` does not transmit the coordinator session to it. The activation
page uses a same-origin referrer policy: it suppresses referrers to the
optional Workbench link while allowing stock Chromium to send its exact
Origin on local form POSTs. `Origin: null` remains rejected. The activation
secret is generated in process memory, expires after five minutes, and is
carried in the URL fragment, which the browser removes before exchanging it.
A successful exchange creates one `HttpOnly`, `SameSite=Strict` browser session
in process memory. The session expires after eight hours or when the process
stops; **End local session** revokes it immediately. Re-entry after expiry or
logout requires restarting the explicit local companion for a new one-use
activation. No Workbench bearer token grants access. All unsafe UI requests require
the exact local Origin and a session CSRF token. The coordinator socket remains
protected by its own owner-only permissions. Browser Host and peer checks are
defense in depth, not the authorization boundary against a reverse proxy.

No service, worker, Docker runtime, or coordinator job starts merely by
importing these modules. This local companion is never started automatically.
It is not a general standalone product or a replacement for the Workbench
conversation. Actual activation and any service setup require separate review.

The overview reads API health, capacity, a bounded 25-job list, and each listed
current attempt's last runtime observation timestamp. Record update time is
labeled separately. The detail page shows immutable job identity, selected
worker/profile/workspace and resources, orphan policy, attempt ownership and
observation identifiers, recovery capability, and bounded durable event replay.
Its read-only status hint never replaces controls or unfinished input. A newer
version prompts a manual refresh. Closing the page does not change coordinator
or worker state.

Job submission starts from a v1 JSON template with registered policy keys. The
API does not publish a profile catalog, so the operator enters keys from the
local policy. Validation returns a canonical full spec; the operator reviews
its target and payload before an explicit submission. Cancel, eligible retry,
and verified reattach forms send the displayed job version and a unique
idempotency key. After an uncertain response, the page offers replay with the
exact same payload, version, and key. A conflict requires refreshing the job.
The coordinator remains the final authority for eligibility and policy.

Attempt evidence is offered only after positive stop evidence. Logs read at
most 4 KiB per page from the post-stop bounded export and flag possible
missing earlier output. Artifact manifest metadata and hashes remain visible
when logs are unavailable; the view does not stream artifact contents. Unknown
visibility and unconfirmed outcomes remain explicitly unknown. It does not
automate FLOW work or access the coordinator store, supervisor journal, or
Docker directly.
