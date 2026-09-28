# Local work-item workflow

FLOW is a staged local workflow described by [the design record](adr-flow-work-items.md).
The tracker retains the issue; a deliberately created private metadata repository
holds the human-authored local queue. Implementation of registry, transactions,
commands and code workspace preparation is tracked in #110, #111, #112 and #119.
The local registry, checkpoint layer, task commands, and explicit local code
workspace preparation are implemented in separate stacked changes.

The initial local registry uses `ai-workbench work init --root PATH` to select a
new private metadata Git repository, with `--github-host`, `--github-repo`, and
`--jira-site alias=https://site.example.com` source mappings. The private
profile lives outside Git; `WB_FLOW_PROFILE` or `--profile` selects it. `work
open REFERENCE` explicitly creates a missing metadata workspace; `--dry-run`
previews it. `work show` and `work list` only read registered work items.
`work open PR_URL --link-to ISSUE_URL` explicitly aliases a linked PR to the
issue's existing queue. A canonical URL or qualified `github:host/owner/repo#N`
and `jira:site:KEY` identifies a source; a bare shorthand must resolve to one
configured source. Opening metadata never clones a code checkout.

`ai-workbench tasks list|show|next|history|reconcile` inspect one registered
item; `reconcile` requires `--dry-run` and reports checked claims, missing IDs,
and dirty target content without inferring completion. `next` only recommends
an eligible task. `tasks add|update|block|complete|cancel|supersede|reopen`
change the selected metadata file through the checkpoint layer. `assign-id`
gives one unambiguous manual title a stable ID after a preview. Mutations accept
`--preview`, `--expected-revision`, `--expected-hash`, and `--operation-id`;
carry the preview's operation ID and version into the write so generated IDs and
retry results stay stable. `complete` needs `--evidence`; `cancel` and
`supersede` need `--reason`; `reopen` needs an explicit historical commit ID.
Use `--checkpoint-pending` only after reviewing manual edits that should be
retained before a terminal removal. JSON is the default output; `--format text`
provides formatted output. The commands never start an agent or contact a
tracker or Workbench API, and their checkpoint reports `publication: pending`.

## Small item

```markdown
# Tasks

<!-- FLOW work-item: wi-example-42 -->
<!-- Source: https://github.com/example-org/example-repo/issues/42 -->

## P2

- [ ] Fix the heading typo
```

This manual task is readable without metadata. Before a tool changes or links
it, the tool previews an explicit `**ID**` insertion. Opening the metadata
workspace never clones a code repository or starts an agent.

## Substantial item

```markdown
# Tasks

<!-- FLOW work-item: wi-example-43 -->
<!-- Source: https://github.com/example-org/example-repo/issues/43 -->

## P1

- [ ] Add a parser regression test
  - **ID**: parser-regression-01
  - **Details**: Cover multiline input and preserve unknown fields.
  - **Files**: `tests/test_parser.py`
  - **Acceptance**: The test fails before the repair and passes after it.

- [ ] Repair parsing
  - **ID**: parser-repair-02
  - **Blocked by**: parser-regression-01
  - **Acceptance**: Relevant tests pass and the result is reviewed.
```

Adding either generated task must return an actual metadata commit. A material
acceptance change is checkpointed before an explicit completion removes the
current block. Completion records outcome, actor and evidence; cancel and
supersede record different outcomes and do not satisfy a dependent task. If a
blocker disappears without successful evidence, the dependent task stays
unresolved. An empty queue retains the header and identity preamble.

## Task relationships

Use `--parent-task-id ID` on `tasks add` or `tasks update` to place a task under
an active task in the same work item. `--clear-parent` removes that link on
update. The `Parent task` field organizes the local view; it never makes a
child ready or completes the parent. A parent with active children cannot be
removed through complete, cancel or supersede until its children leave the
active queue or are deliberately reparented. Cancellation remains distinct
from completion, and completing the parent always requires its own evidence.

Use repeated `--blocked-by ID` arguments to record prerequisites when adding
or updating a task. `--clear-blocked-by` removes all dependencies on update.
The same `blocked_by` list, `parent_task_id`, and `clear_parent` arguments are
available through `flow_task_preview`/`flow_task_apply`; an empty `blocked_by`
list clears dependencies. Preview and apply use the same revision, document
hash, operation ID and checkpoint rules as other task edits. Newly set IDs must
refer to active tasks or tasks with recorded completion in the same work item;
unknown or canceled IDs are rejected. Existing manually authored unknown IDs
remain unresolved, never satisfied by absence. Cycles and self-links are
rejected. A dependency is satisfied only by recorded completion; reopening the
prerequisite blocks the dependent task again.

`tasks list --format text` indents active children, while JSON task reads and
bounded packets expose parent and active-child IDs. `next` still suggests the
highest-priority unblocked task; parentage alone does not affect readiness. For
a phase gate, add explicit dependencies on the required child IDs. These local
relationships never create source-tracker subtasks or publish private task
structure to the central API.

## Source issue status

FLOW's local capture, read, preparation, and task checkpoint commands do not
change Jira or another source tracker. A read-only resume or `next` suggestion
does not start work. When an agent is separately authorized to carry out work
on a linked issue, it follows the operator's project-specific status workflow
at work start and handoff through the source tracker's own interface. That
requires reading the issue's current status and available transitions;
requirements and completion evidence still govern the choice. A local FLOW
completion, commit, or PR does not automatically close a source issue or imply
deployment. Agents must not copy private FLOW task IDs or breakdowns into the
source tracker. This guidance also applies to one-step work without FLOW;
FLOW itself has no universal status mapping or automatic synchronization.

`CONTEXT.md` can say what was decided, the next step and a handoff. `SPEC.md`
and `PLAN.md` are useful only when the work merits them. A review handoff
reports the exact code revision separately from the task document. A fresh
session reads the bounded local context; it need not replay a transcript.

Manual `[x]` and deletion are reconciliation signals. Foreign `TODO.md`, Spec
Kit checklists and arbitrary Markdown remain read-only until a person previews
and explicitly adopts them. Filename case is not a format discriminator.
Do not place private paths or runtime IDs in tracked documents. Metadata Git
history stays local until a separate private backup is deliberately chosen.

## Local code workspace preparation

Use `ai-workbench work bind REFERENCE --name NAME --repository ABSOLUTE_CLONE
--worktree-root ABSOLUTE_DIR --base-ref REF --branch PROJECT_BRANCH` to record a verified local mapping
outside the metadata repository. The worktree root must exist. Each item may
have multiple named repository bindings. The chosen base ref is resolved to an
exact commit before a new branch is created, regardless of the invoking
checkout's current branch. `--branch` is required so the user can select a name
that follows the source project's conventions. FLOW identifiers and task files
belong only in the private metadata repository, not in source branches, commits,
pull requests, or tracker updates. Existing bindings retain their recorded branch;
there is no automatic rename. A missing local clone can be created
only from a configured HTTPS `--remote` when the binding has
`--allow-remote-read`; normal local resume performs no fetch and labels remote
freshness unknown. There is no default remote push.

`ai-workbench work prepare REFERENCE` reuses a recorded worktree or creates
only missing local branches/worktrees. `work resume` reads the recorded context
without creating one. The bounded result includes per-repository branch, HEAD,
starting commit, dirty/divergence state, a local task summary, and the next
human-directed step. Linked PR enrichment and an agent session are marked
unavailable rather than inferred. If a matching branch or worktree predates
the relation, preparation stops with an ownership warning. To associate a
verified existing worktree deliberately, use `work adopt REFERENCE --name NAME
--worktree ABSOLUTE_PATH`; this records the selected branch and current HEAD,
but labels its actual original starting commit and active ownership unknown.

Preparation uses repository locks in a private per-user state directory and
holds the profile lock only for short registry updates, so a slow authorized
clone does not block unrelated FLOW writes. It records a pending base before a
Git worktree write and preserves successful sibling repositories if another fails. Retry
after inspecting an error; it can recover a matching interrupted worktree
without deleting user files. It suppresses Git hooks and rejects checkout
filters, since a checkout may otherwise execute local programs. It does not
install dependencies, run setup scripts or services, start agents, restore an
autonomous queue, push, open PRs, change trackers, or deploy. Worktree paths,
clone locations and recovery state live in a protected local sidecar beside the
FLOW profile, never in `TASKS.md`.

The metadata repository stays on its retained `flow-history` branch. A task
mutation returns the actual checkpoint and a `publication: pending` marker;
that marker means any later Workbench API event still needs publication. The
host-local operation journal beside the profile makes retries with the same
operation ID idempotent after a successful Git commit. If Git or a hook fails,
the authored document and journal remain for inspection. Resolve the cause,
then retry the same operation and expected version; do not reset, clean, or
rebase the metadata repository to hide a failed checkpoint. A changed target,
unexpected staged file, or conflicting Git history requires manual review.

Local commits are not off-device backups. For a deliberate private backup,
inspect the repository and use `git bundle create PATH flow-history` to write a
bundle to a chosen protected destination. Restore into a separate directory
with `git clone PATH RESTORE_DIRECTORY`, inspect `flow-history` and the private
profile/registry separately, then point a new profile at the restored root.
The bundle contains committed metadata, not the host-local profile, registry,
or pending operation journal; back those up separately with restricted access.

## Optional central relation projection

An authenticated operator can deliberately publish a small projection with
`POST /api/flow/work-items`: stable work-item ID, plain HTTPS source reference,
document commit revision and hash, selected task IDs, operation ID, and the
expected projection version for updates. The source reference must identify one
work item; a second ID for the same source conflicts. The API stores no task
text, local paths, or document body. This endpoint does not accept or complete
an action, change a source tracker, or clear the local `publication: pending`
marker by itself. Automated publication and offline replay are future work.

`GET /api/flow/work-items` and `GET /api/flow/work-items/{id}` expose the
projection to authenticated operators. To relate an existing action, post its
exact ID to `/api/flow/work-items/{id}/actions/{action_id}/link` with an
operation ID and current work-item and action versions. The action retains its
original source attribution, status, scope and version. A later published
document revision marks the link as `definition_drift`; a repeat link then
requires deliberate reconciliation. A retry with the same operation ID and
identical request returns its original result. Changed content under that ID
conflicts. Version conflicts require rereading both records.
