# Local work-item workflow

FLOW is a staged local workflow described by [the design record](adr-flow-work-items.md).
The tracker retains the issue; a deliberately created private metadata repository
holds the human-authored local queue. Implementation of registry, transactions,
commands and code workspace preparation is tracked in #110, #111, #112 and #119.
The local registry and checkpoint layer are implemented in the first stacked
changes. Task commands and code workspace preparation follow in later changes.

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

`CONTEXT.md` can say what was decided, the next step and a handoff. `SPEC.md`
and `PLAN.md` are useful only when the work merits them. A review handoff
reports the exact code revision separately from the task document. A fresh
session reads the bounded local context; it need not replay a transcript.

Manual `[x]` and deletion are reconciliation signals. Foreign `TODO.md`, Spec
Kit checklists and arbitrary Markdown remain read-only until a person previews
and explicitly adopts them. Filename case is not a format discriminator.
Do not place private paths or runtime IDs in tracked documents. Metadata Git
history stays local until a separate private backup is deliberately chosen.

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
