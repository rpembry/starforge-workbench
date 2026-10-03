# Local coordinator browser view

The Workbench coordinator view is an explicit local opt-in over the versioned
coordinator Unix socket API. Set `WB_COORDINATOR_UI_SOCKET` to the absolute path
of the owner's protected coordinator socket when starting a **local-mode**
Workbench instance. The view is then linked from the dashboard at
`/coordinator`. It is not mounted in Cloudflare mode and rejects requests whose
Host or direct client address is not local. Keep the owner socket out of public
proxy configurations. The existing local Workbench browser authentication still
requires an operator bearer credential.

The view reads API health, capacity, a bounded job list, job/attempt state,
recovery capability, and bounded event replay. It sends no request that starts
work while merely loading a page. Job submission requires a registered v1 spec
and explicit confirmation. Cancel, retry, and reattach forms send the displayed
job version and a unique idempotency key; the coordinator enforces conflicts.
If a request's delivery is uncertain, the page offers replay with the exact
same payload, version, and key. An HTTP conflict directs the operator to refresh.

Attempt evidence is offered only after the coordinator reports positive stop
evidence. Logs read at most 4 KiB per page from the coordinator's post-stop
bounded export, and the page flags possible missing earlier output. The artifact
manifest lists verified metadata; the view does not stream artifact contents.
Unknown visibility and unconfirmed outcomes are shown as such. This view does
not automate FLOW work or imply that a job completed successfully.

The browser adapter is intentionally thin: it calls `CoordinatorClient`, with
no access to the coordinator store, supervisor journal, or Docker. Tests use a
fake client and synthetic records. Enabling the view does not launch a
coordinator service or worker.
