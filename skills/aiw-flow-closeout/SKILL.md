---
name: aiw-flow-closeout
description: Reconcile and checkpoint the requested FLOW work-session outcome while preserving unfinished or review-pending tasks. Use at explicit session closeout, not to select the next task automatically.
---

# Close out a FLOW work session

Read the selected work item's tasks, history, current document revision, and any reported test/review evidence. Use `flow_task_read` (`list`, `history`, `reconcile`) or the equivalent `ai-workbench tasks` commands. A checked box, source tracker state, process activity, or agent claim does not independently establish completion. Preserve blockers and unfinished/review-pending tasks.

For each outcome actually authorized and supported by evidence, preview then apply the exact `complete`, `cancel`, `supersede`, `reopen`, `update`, or `block` edit with a stable operation ID and current revision/hash. Verify the checkpoint and inspect the resulting list/history. On stale or uncertain writes, use `flow_operation_status` and current state before any retry; never bypass a denial through the CLI. A local outcome remains `publication: pending` until the separate central relation/publication workflow is completed.

For authorized work on a source-linked issue, follow the operator's project-specific tracker instructions at review handoff or completion. Read the current issue, available transitions, required fields, and evidence before making a separate tracker status change; then verify its resulting status. Do not infer that local FLOW completion means the source issue is closed or deployed. If no status workflow is authorized for that source, prepare an update only when requested. Never publish private FLOW task details to the tracker or implicitly complete a linked Workbench action.

Create a bounded handoff packet when unfinished work needs one. Report completed local outcomes, evidence and limitations, remaining work, and pending publication. End after this selected scope; do not start another task or agent session.
