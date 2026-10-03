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

Review the private output locally. `ai-workbench titles apply PLAN_ID
--context CONTEXT` selects exact preview rows, including a custom existing
title marked `needs_choice`. `--all-eligible` selects only rows without a
custom existing title or conflict. `ai-workbench titles status PLAN_ID` reads
retained per-row outcomes. Changes to a binding, configured label, or old
title invalidate the previewed row. An outcome of `unknown` requires status
and a fresh read before retry. Preview, startup, attach, and status never
trigger a Codex title write. These commands never infer a thread from a title,
directory alone, or recency.

`ai-workbench titles undo PLAN_ID CONTEXT` explicitly restores a verified
prior title only while the current title still equals the title written by
that plan. A later manual edit blocks undo. A blank prior title also requires
manual reconciliation. MCP exposes `codex_title_undo` under the same local
apply opt-in.

## Provider write limitation

On 2026-10-03, installed `codex-cli 0.155.1` generated an app-server schema
with `thread/name/set` accepting only `threadId` and `name`. It has no expected
old title or revision argument. A separate `thread/read` followed by
`thread/name/set` leaves a race with Codex desktop, CLI, or other clients.
Workbench therefore reports `blocked` with reason
`provider_has_no_atomic_expected_title_rename` for a real eligible row and
does not issue the rename. The catalog uses SQLite `mode=ro` for metadata,
consistent with the launcher's existing binding validation. It never edits
Codex's database or session files, touches an active writer lock, injects
keystrokes, or restarts Codex. A supported provider API with atomic
compare-and-set title semantics is required before enabling the adapter.
The synthetic guarded adapter in tests proves Workbench's plan and readback
logic, not live Codex rename capability.

This release offers CLI review. The central dashboard has no host-local
Codex title writer or trusted exact tab-title observer, so it has no rename
control. A later UI can consume the same plan and status operations after
those capabilities become available.

## MCP and installation

`wb-mcp` exposes typed `codex_title_preview`, `codex_title_status`, and
`codex_title_apply` tools against the same private state and code path as the
CLI. The apply tool additionally needs local
`WB_MCP_ALLOW_TITLE_APPLY=1`; the Codex provider guard still blocks actual
writes. A denied or uncertain MCP result must not be retried through CLI
without reading the same plan and refreshing provider state. No Workbench
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
issues, or PR text. A live rename smoke remains unverified and needs both an
atomic provider API and explicit authorization for a disposable thread.
