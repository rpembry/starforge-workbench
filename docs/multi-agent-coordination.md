# Human-directed agent coordination

Randall directs this project's engineering work through conversations and GitHub. AI agents contribute proposals, implementation, and review within that direction. This is a personal workflow others can adapt, not an autonomous agent orchestration service built into Workbench.

GitHub is the durable engineering decision record: problems, scope, assignments, review findings, pull requests, and acceptance. Workbench complements it with accepted actions, run observations, operational events, and attention. The software being developed helps observe the work of developing it.

## Roles and authority

Claude Code, Codex, and OpenCode can each propose, implement, or review. These are assigned roles, not permanent capabilities or a fixed hierarchy. Earlier work paired Claude's advisory review with Codex implementation; later work used Codex review and OpenCode implementation. A model choice belongs to a particular assignment and does not grant authority.

An issue proposal or an agent's recommendation does not authorize itself. Randall's explicit direction establishes the scope, priority, executor, and any limits. A Ready issue is one way to record that direction; a direct request to implement an issue also suffices. Record the relevant instruction in the issue when needed so another session can understand the assignment. Do not demand a redundant approval merely because a board field has not caught up.

Within authorized scope, an agent can carry work through implementation and verification, and can pass instructions or review findings to another agent when Randall has authorized that coordination. Agent-to-agent handoffs are a workflow practice, not an automatic Workbench transport. A proposer may implement after assignment; there is no mandatory separate-agent approval ritual. A separate review can be useful for consequential changes, but a second model's agreement is not evidence that the code works.

Merge, deployment, service changes, live-provider tests, and enabling notifications have different effects. Follow the authority actually given for each; do not infer deployment permission from implementation or merge permission. Explicit standing authorization continues to apply.

## A repeatable engineering cycle

1. Capture a concrete issue with acceptance criteria, dependencies, provenance, and known uncertainty. Choose one canonical issue and link related work instead of maintaining competing backlogs.
2. Record the assignment and its boundaries. Give the implementing agent the repository, issue, base branch or dependency PR, expected checks, and any limits on live systems. Use an isolated checkout when another agent is modifying the repository; never have two writers share a checkout.
3. Implement a focused change, commit and push it, and open a PR linked to the issue. Report the exact commit tested, checks performed, and environmental skips. Fixtures establish only the behavior they exercise.
4. Review the current PR diff and reproduce actionable concerns. Put durable findings on the PR or canonical issue, with the failure case, expected behavior, and required regression coverage. Return those findings to the assigned worker within the authorized scope.
5. Verify the revised head. For stacked PRs, reconcile their bases and test the combined result before merging. Do not carry an earlier clean review forward across unreviewed changes.
6. Merge when authorized and ready. Close the issue when its acceptance criteria are satisfied; keep outstanding acceptance work open or link a clearly scoped follow-up. A merged collector with synthetic tests does not establish live-provider compatibility, and a merge does not mean deployed.

A handoff should contain enough information to resume without reconstructing the conversation: issue and PR links, exact branch/head, what is complete, remaining findings, test evidence, and the next authorized action. Keep credentials, raw transcripts, private inventories, and user content out of public handoffs.

## What belongs where

| Record | Purpose and limits |
| --- | --- |
| GitHub issue and comments | Engineering scope, proposal provenance, assignment, decisions, unresolved acceptance work. A label or assignee alone does not grant new authority. |
| Pull request and review | Proposed source changes, evidence tied to a commit, review findings, and merge outcome. |
| Workbench action | An explicit commitment with an actor and status. A proposal remains proposed until accepted under user direction. |
| Workbench run | An observed process lifetime and any explicit action association. A heartbeat shows presence, not progress or completion. |
| Workbench event and artifact | A concise attributed observation, decision, or accomplishment and supporting references. Reported accomplishments remain distinguishable from verified outcomes. |

Use the authenticated API for Workbench records; read the current schema and record versions before unfamiliar writes. Reconcile conflicts instead of blindly retrying or editing the database. Link exact action/run identities when known. Similar titles, a shared directory, an idle terminal, or missing collector evidence do not establish a relationship or completion. GitHub and Workbench do not automatically synchronize these decisions.

## Provenance without a schema expansion

The existing models already distinguish action `actor`, action/event `source` and `source_id`, and run `provider`, `actor`, `source`, and `source_id`. Events can reference an `action_id` and `run_id`; artifacts can retain supporting links. These fields have different meanings: the assigned actor is not necessarily the proposer, and a collector's source identifies the reporting channel, not the author of the underlying work.

Keep proposal authorship in GitHub's author/comment history and existing source labels where applicable. In Workbench, preserve the true source and stable source identity; an attributed proposal or decision event can explain who suggested it and reference the issue. Never overwrite import or collector provenance just to identify the current implementer. Retries must reuse the same immutable event identity only for the same content.

No `source_agent` field or database migration is added for this workflow. A dedicated field would need a concrete query or UI requirement and a clear distinction between proposer, implementer, reviewer, and reporter. Revisit that design only when the existing attribution cannot support a real use case.

## Adapting the pattern

Choose your own tools and roles, keep the engineering decision record readable, and preserve the separation between user direction and observed activity. The useful pattern is explicit scope, durable handoffs, isolated implementation, and evidence-based review. There is no requirement to run multiple agents or reproduce Randall's desktop setup.
