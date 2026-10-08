# Offline synthetic search approval

Issue #289 describes a prospective query leak surface. The installed-model
synthetic adapter currently has no search capability and keeps its existing
network sandbox. This change prepares an optional offline capability; it does
not replace an observed active search leak, pause real inference, connect an
engine, or provide production human authentication.

Search queries **are outbound sensitive payloads**. A classifier, regex, model
response, MCP sampling interaction, operator label or boolean is not consent.
The wrapper reuses `ReleaseBroker` and the synthetic human-session authority's
exact payload/destination/job/attempt/operation/revision/expiry/decision binding,
durable receipts and revocation tombstones. The canonical binding protocol stays
`diagnostic.release.v1`; the registered destination is `search.synthetic`.
The default authority still allows only `coordinator.synthetic`. Search requires
explicit composition of an authority limited to the search destination, a fake
endpoint bound to that same destination, and the current supervisor fence.
No caller chooses a URL, engine, redirect target or approval scope.

`SearchApprovalBroker` owns one serial query for an existing accepted diagnostic
attempt. The trusted fixture owner supplies existing private directories for
the wrapper, its `query` child, authority, and fake inbox. Authority and inbox
storage cannot overlap the wrapper. A query is at most 1,024 UTF-8 bytes and
eight pre-send revisions. Identical pending proposals are idempotent. Identical
rejected-query retries return
fixed `declined` without allocating another revision or adding audit records.
Changing bytes revokes prior approval and creates a fresh operation/revision; no
session-wide approval or model-driven allocation of more query slots exists.
Future owner composition must bound any additional slots itself.

The proposal-only `SyntheticSearchTool` returns a fixed
`diagnostic.search.status.v1` schema: `disabled`, `pending`, `declined`, `ready`,
`failed`, `cancelled` or `uncertain`. It cannot approve, dispatch or read results.
`build_server(search_tool=...)` explicitly registers `search_with_approval` with
only a query argument. The ordinary server and installed CLI do not construct
or register it; there is no new startup mode or setting. Direct search/retrieval
aliases are denied by the VM policy and MCP's exact tool/argument allowlist.
This synthetic facade must remain on a local fixture channel: sending a query
as arguments through a remote MCP client would itself disclose the query.
No query, receipt, review credential, result, audit count or operation ID is
returned by the MCP tool. The diagnostic trial reporter is unchanged and still
emits only its approved protocol/status schema.

The local owner calls `review()` to inspect the exact `ReviewSnapshot` bytes,
destination and revision, then `decide()` with the existing instance-issued
synthetic-human capability. Tests deliberately label that decision simulated.
There is no search browser UI, terminal prompt, login or sampling-based approval
in this slice. Machine/service tokens and caller-claimed identities cannot grant
approval. Production still requires a separately reviewed human-only session,
presence and authority owner inaccessible to the model and browser automation.
The same OS user or an administrator can modify the fixture, so private files
and Python objects are not an access-control sandbox against those actors.

Only the trusted broker's explicit `dispatch()` sends the approved query to the
fixed fake inbox. Fsynced intent precedes the side effect; uncertain/crashed
operations reconcile the exact operation and never blindly resend. A rejected
query yields fixed `declined`, never fabricated empty results. Expiry, edit,
revocation, ownership loss and cancellation deny a fresh send. Canonical
supervisor fencing serializes every wrapper operation with cancellation. Local
cancellation also blocks future result consumption; it cannot recall a query
already accepted while the owner was active. No queue, scheduler, guest lifecycle
or network policy is replaced.

The fake transport supplies at most 4,096 bytes of strict UTF-8 **untrusted text**.
Only `local_result()` exposes it to the local context owner after exact fake
acceptance, tagged `diagnostic.search.result.v1` with `untrusted: true`. The
fixture does not wire it into a real Qwen inference step. URLs, redirects,
instructions or credential requests in results stay inert; no fetch, URL parser,
HTTP redirect handling or SSRF-capable transport is supplied. Results cannot
approve another query, open another tool, or release themselves automatically.
A future real broker must independently enforce fixed endpoint/address scope,
redirect denial, bounded resource/cancellation handling and untrusted-result
isolation while preserving the worker's disabled network.

Raw query/revision and decision evidence stays in a mode-0600 private audit,
bounded to 32 events and 64 KiB; capacity exhaustion denies further review/send.
Consent that cannot be durably audited is revoked before use. Audit and status
are separate: no query telemetry or automatic audit export is added. Tested
adapter/parser/credential failures become fixed errors without application
stdout/stderr or query/result canaries in logs. The existing authority receipt
quota and endpoint inbox bounds remain in force. Whole-authority rollback and
external client/proxy instrumentation remain outside the synthetic threat model.

No notifications are implemented. If Pushover or another remote notifier is
added later, it must carry neither query text nor approval secrets, and a
content-free alert must not authorize dispatch. No real search, authentication,
service, account, ACL, settings or network changes are included here. Independent
exact-head synthetic QA is required before accepting this milestone; it does
not establish production privacy or real-engine compatibility.
