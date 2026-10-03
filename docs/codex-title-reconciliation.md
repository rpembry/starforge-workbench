# Codex title reconciliation

`ai-workbench titles preview CONTEXT [CONTEXT ...]` reads configured labels,
private exact Codex bindings, and a read-only Codex thread catalog. It writes
only a private Workbench plan beneath
`~/.local/state/starforge-ai-workbench/title-reconcile/`. It does not rename a
thread. Each row shows the stable thread ID, configured and observed labels,
current and desired title, and eligibility or conflict reason. The current
adapter cannot reliably observe a terminal tab label, so `observed_label: null`
means unknown, not agreement. If a trustworthy exact-context tab observer is
added, disagreement must block the row.

The catalog and supported app-server rename are pinned to the same resolved
Codex home (`CODEX_HOME` when set, otherwise the user's `~/.codex`). Changing
that store between preview and apply invalidates the row. The store path is
retained only in private plan state, not returned in CLI/MCP row output.

Review the private output locally. `ai-workbench titles apply PLAN_ID
--context CONTEXT` selects exact preview rows, including a custom existing
title marked `needs_choice`. `--all-eligible` selects only rows without a
custom existing title or conflict. `ai-workbench titles status PLAN_ID` reads
retained per-row outcomes. Changes to a binding, configured label, or old
title invalidate the previewed row. An outcome of `unknown` leaves a protected
per-thread unresolved intent; later plans, retries, and undo remain blocked
until provider operation termination can be established through a future
supported recovery path. A fresh title read alone cannot prove that a delayed
request will not still apply. The intent is persisted before sending; verified
write evidence and outcome are persisted before removing it. A failed audit
write leaves the intent in place. Preview, startup, attach, and status never
trigger a Codex title write. These commands never infer a thread from a title,
directory alone, or recency.

Strict apply remains the default and blocks real writes because Codex has no
atomic expected-title operation. The operator-approved practical mode uses
`ai-workbench titles apply PLAN_ID --context CONTEXT --mode practical
--confirm-non-atomic` (or `--all-eligible`). It refreshes each selected row
immediately before its request, calls Codex app-server `thread/name/set`, and
reads the title back. The confirmation acknowledges that another client can
rename the thread between the fresh check and write, so a concurrent manual
title may be overwritten. This mode must not be described as atomic or
fully protected from concurrent writers. Unknown outcomes do not auto-retry.

`ai-workbench titles undo PLAN_ID CONTEXT` explicitly restores a verified
prior title only while the current title still equals the title written by
that plan. A later manual edit blocks undo. A blank prior title also requires
manual reconciliation. MCP exposes `codex_title_undo` under the same local
apply opt-in. Practical undo likewise requires `--mode practical
--confirm-non-atomic` and has the same race after its fresh check.

## Provider write limitation

On 2026-10-03, installed `codex-cli 0.155.1` generated an app-server schema
with `thread/name/set` accepting only `threadId` and `name`. It has no expected
old title or revision argument. A separate `thread/read` followed by
`thread/name/set` leaves a race with Codex desktop, CLI, or other clients.
Strict mode therefore reports `blocked` with reason
`provider_has_no_atomic_expected_title_rename` for a real eligible row.
The approved practical mode uses the supported app-server method with the
disclosed race. The catalog uses SQLite `mode=ro` for metadata,
consistent with the launcher's existing binding validation. It never edits
Codex's database or session files, touches an active writer lock, injects
keystrokes, or restarts an existing Codex process. A supported provider API
with atomic compare-and-set title semantics would enable the stronger default.
Synthetic guarded-adapter tests prove Workbench's plan and readback logic,
not live Codex rename capability.

This release offers CLI review. The central dashboard has no host-local
Codex title writer or trusted exact tab-title observer, so it has no rename
control. A later UI can consume the same plan and status operations after
those capabilities become available.

## MCP and installation

`wb-mcp` exposes typed `codex_title_preview`, `codex_title_status`, and
`codex_title_apply`, and `codex_title_undo` tools against the same private state and code path as the
CLI. The apply tool additionally needs local
`WB_MCP_ALLOW_TITLE_APPLY=1`; practical apply and undo additionally need
`mode=practical` and `confirm_non_atomic=true`. A denied MCP result cannot be
bypassed through CLI. An uncertain result blocks later writes through both
surfaces until a supported terminal-outcome recovery is available. No Workbench
server deployment is needed for the host-local CLI. Existing MCP clients
need a reviewed upgrade and reconnect before these tools appear. Check
`workbench_capabilities` on the running server, and inspect the client's tool
list. Source code and fixture tests alone do not prove installation.

Workbench's shipped skills presently cover FLOW capture, resume, handoff,
and closeout. None owns launcher title management, so this workflow remains
documented here and in MCP tool descriptions rather than adding an unrelated
instruction to a FLOW skill. If a future launcher workflow skill is shipped,
it should teach exact selection, conflict review, approval, readback, and
uncertain outcome recovery. Follow [skill installation](skills-install.md):
preview/update one managed skill from a committed checkout; customized copies
cause a conflict and are never overwritten automatically. There is no global
skill installation as part of this feature.

For disposable QA, use a synthetic manifest, binding state, and test catalog.
Do not use real thread IDs or private labels in public fixtures, screenshots,
issues, or PR text. A live rename smoke remains unverified and needs explicit
authorization for a disposable thread and a reviewed preview.
