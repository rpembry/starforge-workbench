---
name: aiw-flow-capture
description: Capture an authorized substantive request as a durable FLOW task for a deliberately selected issue. Use for explicit task capture; do not use for casual questions or brainstorming.
---

# Capture FLOW work

Resolve the selected issue and configured private local FLOW profile. Preserve the user's intent, constraints, acceptance criteria, and source identity in a concise task; do not copy a raw prompt or transcript. A one-step request needs no FLOW item. Do not invent an issue or turn a suggestion into accepted work. A project's existing task-file convention is separate from FLOW; do not create FLOW files or identifiers in a source repository, tracker, or PR.

Prefer correctly scoped `flow_item_show` and `flow_task_read` to find existing work. If needed and enabled, preview then deliberately create with `flow_item_open`. Read the current task revision and document hash, then use `flow_task_preview` (`add`, stable operation ID, title, details, acceptance, priority) and `flow_task_apply` for the exact proposed change. The CLI equivalents are `ai-workbench work show/open` and `ai-workbench tasks list/add`; they use the same local operations when MCP is unavailable and the request is authorized.

Check the returned checkpoint and reread the task list. A local checkpoint has `publication: pending`; it does not accept a central action or change a source issue. Publication to Workbench is a separate deliberate choice, especially for external-project work. If the profile is unavailable, the mutation is denied, the revision changed, or the write result is uncertain, stop and inspect current state or `flow_operation_status` before retrying. Never use a shell or another interface to bypass a denial. End after capturing the requested work.
