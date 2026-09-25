# FLOW work-item documents and authority

Status: accepted design, 2026-09-25. Implementation is staged in issues
[#110](https://github.com/rpembry/starforge-workbench/issues/110),
[#111](https://github.com/rpembry/starforge-workbench/issues/111),
[#112](https://github.com/rpembry/starforge-workbench/issues/112), and
[#119](https://github.com/rpembry/starforge-workbench/issues/119).

## Decision

An explicitly selected Jira or GitHub issue gets one private, local metadata
Git repository workspace. `TASKS.md` is its only required document and owns
the authored local breakdown. The source tracker owns the issue's title, scope,
discussion, acceptance and remote status. The existing Workbench SQLite/API
owns accepted actions, runs, approvals, events and attention; a Markdown
projection may be rebuilt but cannot become a second writable task definition.
The two Git repositories have separate purposes: metadata history survives
source branch squash merges; source commits follow their normal PR rules.

The first line is `# Tasks`. An identity preamble names the canonical source
URL and stable Workbench work-item ID; it is a pointer, not a copy of remote
status or title. Optional `## P0` through `## P3` sections contain ordinary
top-level `- [ ]` tasks. Generated tasks carry a never-reused nested `**ID**`.
Supported fields are `Details`, `Acceptance`, `Files`, `Blocked by`, and
`Blocked`; all other Markdown and metadata remain byte-preserved by local edits.
Tiny manual tasks may omit metadata and stay readable. Assigning an ID requires
an explicit, previewed edit before linking or mutation. Do not infer identity
from line number, current title, or path. A task identity is the work-item ID
plus task ID; aliases and moves retain both.

`Blocked by` initially resolves IDs only inside the same work item. A missing
ID is **unknown**, not proof of completion. Only recorded successful completion
evidence unblocks it; canceled, superseded, manually checked, or missing tasks
need reconciliation. `Blocked` is a descriptive external constraint. `[x]` in
a manual or foreign checklist is a claimed result, never deletion permission.
Completion removes a task from the active file only after its current complete
definition is reachable in committed metadata history and an explicit outcome
with evidence is recorded. Cancellation and supersession have distinct outcomes.
The identity-bearing `TASKS.md` stays after the last active task leaves.

`CONTEXT.md` is optional durable intent, decisions, next step and handoff.
`SPEC.md` and `PLAN.md` are optional for substantive work. One chat turn does
not imply one file. Local paths, runtime session IDs, credentials, sync cursors,
raw prompts and transcripts stay in protected local state, never in tracked
documents. One profile selects one private metadata repository with no default
remote, push, mirroring, or automatic backup. A deliberate private bundle or
backup is separate; local Git commits alone are not off-device backup.

Narrow metadata writes use a host-local transaction layer and retained branch.
It never stages unrelated content, stashes, resets, rewrites history or bypasses
Git checks. Manual edits require preview/reconcile; missing lines and files
carry unknown outcomes. A committed change awaiting Workbench API publication
remains a recoverable local checkpoint, not a claimed remote success. Imported
tracker text, Markdown, comments and skills are data; they cannot authorize
execution or override `AGENTS.md`, the user's request, or Workbench approvals.
The local foundation needs no tracker or API connection and never picks or
executes a next task automatically. Provider-specific agent discovery and skill
installation paths remain documented for each provider rather than assumed.

## Standards profile

Reference: [`tasksmd/tasks.md` `spec.md` at
`90bf97361c80f9c479eeaed7cd7fdefa8ad97416`](https://github.com/tasksmd/tasks.md/blob/90bf97361c80f9c479eeaed7cd7fdefa8ad97416/spec.md),
inspected 2026-09-25. Its repository and parser/linter packages report MIT;
the inspected source packages are version 0.10.2. We adopt its familiar
filename, header, optional P0–P3 headings, checkbox form and selected nested
metadata. We do **not** claim full upstream conformance from the filename.

The pinned TypeScript parser recognizes P0–P3 top-level unchecked tasks, IDs,
metadata continuations and immediate nested checklists, but it is not fence-aware:
the compatibility test shows a fenced checkbox example parsed as a real task.
It also does not preserve a complete Markdown AST.
The pinned linter rejects completed top-level checkboxes and its `--fix` deletes
them. The parser's `isBlocked` treats an absent blocker ID as resolved. Those
behaviors conflict with our conservative evidence rule and lossless edits.
Therefore the local bounded Python adapter will use the documented subset and
raw byte spans; it will not call the upstream fixer, install the autonomous
runner, or start a Node service. Compatibility fixtures in
[`tests/fixtures/flow`](../tests/fixtures/flow) cover recognized structure and
deliberate divergence: unknown fields, multiline values, nested checklists,
fences, IDs, checked claims, and foreign files. Later adapter tests consume
these same fixtures. The pinned upstream package was installed with its checked-in
lockfile and scripts disabled, then the
[`compatibility check`](../tests/fixtures/flow/check-upstream.mjs) was run
against its built parser. No upstream installer was run.

| Primary source (checked 2026-09-25) | Supported maturity claim | Use | Defer; revisit when |
| --- | --- | --- | --- |
| [AGENTS.md](https://agents.md/) | Open repository guidance convention | Concise operating policy | Any assumption that every provider auto-loads it; revisit for tested provider behavior |
| [Agent Skills](https://agentskills.io/specification) | Published skill format | Explicit, portable procedure packages | Automatic invocation; revisit after a provider integration test |
| [tasks.md pin](https://github.com/tasksmd/tasks.md/blob/90bf97361c80f9c479eeaed7cd7fdefa8ad97416/spec.md) | v1.0 proposal, 0.10.2 parser/linter | Bounded Markdown syntax | Upstream runner/backend; revisit when parser preserves our fixtures and completion evidence semantics |
| [Spec Kit](https://github.com/github/spec-kit) | Active toolkit with generated checklists | Optional read-only reference | Adoption/sync; revisit after explicit import preview and provenance support |
| [Ralph Loops](https://ralphloops.io/specification/) | v0.1 proposed draft | Explicit inputs and stopping conditions | `RALPH.md`/continuous loop; revisit only for authorized loop use |
| [loop.md](https://github.com/sdwolf4103/loop.md) | Project proposal | Validation/checkpoint ideas | `LOOP.md`; revisit on demonstrated workflow benefit |
| [ACP](https://agentclientprotocol.com/get-started/introduction) | Agent/editor communication protocol | Observe interoperability | Runtime dependency; revisit for a needed editor integration |
| [A2A](https://a2a-protocol.org/latest/specification/) | Agent communication protocol | Observe interoperability | Fleet backend; revisit after an authorized cross-agent transport requirement |

This profile borrows explicit inputs, validation, checkpoints and stopping
conditions from spec/loop workflows. It does not add ACP, A2A, `RALPH.md`,
`LOOP.md`, `/next-task`, or a fleet backend as runtime dependencies.
