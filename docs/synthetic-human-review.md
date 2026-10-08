# Offline synthetic release-review milestone

`synthetic_review_ui.create_synthetic_review_app` is an unmounted, disabled-by-
default fixture. It has no launcher, startup hook, Workbench/MCP registration,
real endpoint, login, credentials provisioning or live configuration. Explicit
composition requires the synthetic authority, canonical supervisor fence,
diagnostic worker and existing fake local release endpoint. The page labels all
approvals as simulated and warns that same-user filesystem/browser access is not
excluded. Nothing in this milestone authenticates a real human.

The local page displays the exact registered destination, job/attempt/operation,
revision and byte length. Escaped text makes controls visible; the hexadecimal
view represents every byte without Unicode/browser normalization. Full diagnostic
detail is available in a collapsed local-only section. The page has no remote
assets or script, and rejects bearer/Cloudflare/service headers, proxies, wrong
host/peer/origin, unknown sessions, duplicate/extra form fields and bad CSRF.
Responses are no-store with same-origin referrers and a restrictive CSP. Central
status is only the approved protocol/status schema; errors never echo submitted
data, report detail or paths. The fixture emits no application logs or console
report. Deployment access/proxy logging must still be explicitly excluded.
Query-bearing requests and trailing-slash redirects are disabled; unmatched
routes/methods return the same content-free failed-status schema. A status claims
report readiness only after verifying the current unchanged local report.

Decisions submit only a short-lived, single-use server-held snapshot handle,
decision and CSRF value. The server rechecks immutable bytes/destination/revision
and current worker/supervisor ownership before recording approve/reject/defer or
revoking. A stale edit, takeover, cancellation, expired session/view or restart
denies. There is no browser dispatch route. Explicit fake dispatch still uses the
broker and canonical fence; no network content release is added.
Session validity is rechecked inside the canonical fence after body/lock waits,
including before revocation and local detail/status reads. All cooperating local
draft edits, decisions, revocations and dispatch must use that same fence.

The synthetic authority accepts only capabilities issued by its own instance for
trusted synthetic-human session fixtures. Machine/service fixtures and caller
labels cannot approve. Exact receipt records are write-once, protected and fsynced;
revocation is a separate durable tombstone outside the mutable broker journal.
Receipt verification survives restart without renewing sessions. Broker/receipt
rollback cannot resurrect a revoked approval while its tombstone survives. Whole-
authority rollback, deletion by the same OS user, browser automation and an
administrator remain outside this fixture's threat model. Checksums detect
accidental/tampered records but are not cryptographic authority against that owner.

## Proposed live boundary: a decision before configuration

Workbench's ordinary `operator` role is shared with its machine/MCP client. A
bearer operator token cannot identify a human. Cloudflare auth already validates
browser-user versus service claims, but both can map to `operator`; a production
adapter must preserve a verified authentication kind and reject every service or
bearer credential regardless of role. Do not infer human identity from a supplied
actor string, email field, header, or click alone.

| Candidate boundary | Benefit | Required separation / unresolved risk |
| --- | --- | --- |
| Dedicated local review OS user/service | Keeps review sessions and durable authority state outside agent-readable storage; private local UI can avoid proxying details through cloud infrastructure. | Requires separately approved existing-or-new account/service ownership and IPC/mount policy. Agents must lack impersonation/sudo/ACL access and inherited handles. A same-user agent or browser-control tool able to use the review browser can still approve. Dedicated user alone does not prove human presence. |
| Existing verified Cloudflare browser principal | Reuses an existing human identity rather than adding accounts; can explicitly distinguish service credentials at authentication time. | Operator role alone is insufficient. Agent access to the user's browser session defeats separation. Serving report/preview detail through a Cloudflare-proxied app would move local detail through cloud infrastructure. An identity-only bridge to a separate local review service would need a short-lived audience-bound assertion and independent review; that bridge is not implemented or configured here. |

The recommended architecture combines protected local review/authority ownership
with an independently verified human identity or presence channel, while keeping
reports and exact-byte preview local. Reuse existing Workbench visual/CSRF patterns,
not its shared machine operator authority. An agent may request content-free
review status, but cannot obtain the review session, capability, receipt state or
approval interface. Human revocation, expiry and cancellation must remain
authoritative at dispatch; uncertain already-accepted sends cannot be recalled.

Essential decision: identify the human-only session/presence channel and local
authority owner that agents cannot access, including browser-control access.
Approval for any real identity/service/ACL/credential or configuration changes is
a later step. Until then this factory stays unmounted and synthetic, and no real
release approval or deployment-readiness claim is justified.
