# Human-directed agent coordination

Randall directs this project's engineering work through conversations and GitHub. AI agents contribute proposals, implementation, and review within that direction. This is a personal workflow others can adapt, not an autonomous agent orchestration service built into Workbench.

GitHub is the durable engineering decision record: problems, scope, assignments, review findings, pull requests, and acceptance. Workbench complements it with accepted actions, run observations, operational events, and attention. The software being developed helps observe the work of developing it.

## Why this workflow exists

Randall's daily work spans multiple long-running infrastructure contexts: cloud environments, Terraform, containers, CI/CD, application deployment, production support, and Workbench itself. The useful unit of work is often not a single prompt. It is a persistent conversation that accumulates repository knowledge, tool output, decisions, unfinished steps, and follow-up questions over hours or days.

Using multiple agents improves that work when they contribute different perspectives. The usual pattern is a form of cross-model iterative refinement: one agent implements, another independently critiques the result, and the first evaluates and remediates valid findings. A third model is more useful as an investigator when a task first requires broad dependency tracing or an outside architectural view. The goal is not to maximize the number of model calls; it is to apply additional inference-time effort where independent reasoning can reduce consequential uncertainty.

This working style drives the need for Workbench. Persistent terminal sessions preserve active contexts, while shared actions, observations, artifacts, handoffs, and attention views help Randall recover what each agent is doing and what needs human judgment. Workbench is the human control plane around these conversations. It does not turn model output into authority or treat agent activity as verified completion.

## Roles and authority

Claude Code, Codex, Gemini, and OpenCode can each propose, implement, investigate, or review. These are assigned roles, not permanent capabilities or a fixed hierarchy. A model choice belongs to a particular assignment and does not grant authority.

The current default is optimized for Randall's infrastructure and DevOps work:

| Role | Default agent | Intended contribution |
| --- | --- | --- |
| Director and final authority | Randall | Sets intent, constraints, risk tolerance, and acceptance; resolves disagreement and authorizes consequential actions. |
| Primary engineer | Codex | Inspects the repository, implements focused changes, and uses executable feedback such as tests, builds, linters, Terraform validation and plans, rendered manifests, and provider CLIs. |
| Adversarial reviewer | Claude Code | Reviews the requirements, current diff, test evidence, security posture, failure modes, and maintainability; reports concrete findings rather than automatically directing changes. |
| Investigator and second-opinion architect | Gemini | Traces broad dependencies and system effects, researches ambiguous problems, proposes implementation sequences, and acts as a tiebreaker when the builder and reviewer disagree. |
| Additional or local worker | OpenCode | Takes explicitly assigned implementation or review work where its provider, local execution, cost, privacy, or specialization is a better fit. |

This division reflects observed fit and current working preference, not a claim that one model is universally better. Codex is the usual implementation partner because infrastructure work often has strong machine-checkable feedback and benefits from an agent that can repeatedly inspect, change, run, and correct. Claude supplies a deliberately independent critique. Gemini is invoked selectively when repository-wide investigation, external context, or a genuinely independent architectural view adds more value than a third pass over the same diff.

Roles can be reversed for an experiment or a particular task. Earlier work has paired Claude's advisory review with Codex implementation and has also used Codex review with OpenCode implementation. The assignment and evidence should determine confidence, not the provider name.

An issue proposal or an agent's recommendation does not authorize itself. Randall's explicit direction establishes the scope, priority, executor, and any limits. A Ready issue is one way to record that direction; a direct request to implement an issue also suffices. Record the relevant instruction in the issue when needed so another session can understand the assignment. Do not demand a redundant approval merely because a board field has not caught up.

Within authorized scope, an agent can carry work through implementation and verification, and can pass instructions or review findings to another agent when Randall has authorized that coordination. Agent-to-agent handoffs are a workflow practice, not an automatic Workbench transport. A proposer may implement after assignment; there is no mandatory separate-agent approval ritual. A separate review can be useful for consequential changes, but a second model's agreement is not evidence that the code works.

Merge, deployment, service changes, live-provider tests, and enabling notifications have different effects. Follow the authority actually given for each; do not infer deployment permission from implementation or merge permission. Explicit standing authorization continues to apply.

## A repeatable engineering cycle

1. Capture a concrete issue with acceptance criteria, dependencies, provenance, and known uncertainty. Choose one canonical issue and link related work instead of maintaining competing backlogs.
2. Use Gemini or another read-only investigator before implementation when the blast radius, dependencies, or problem definition are unclear. Investigation produces advice and risk questions, not authorization or accepted facts.
3. Record the assignment and its boundaries. Give the implementing agent the repository, issue, base branch or dependency PR, expected checks, and any limits on live systems. Use an isolated checkout when another agent is modifying the repository; never have two writers share a checkout.
4. Have the primary engineer implement a focused change, commit and push it, and open a PR linked to the issue. Report the exact commit tested, checks performed, and environmental skips. Fixtures establish only the behavior they exercise.
5. For changes where independent review is worthwhile, have Claude or another assigned reviewer inspect the requirements, current PR diff, and evidence. Reproduce actionable concerns and put durable findings on the PR or canonical issue, with the failure case, expected behavior, and required regression coverage.
6. Return findings to the primary engineer for independent evaluation. Fix valid findings, explain rejected or deferred findings, and rerun the relevant checks; do not apply reviewer suggestions mechanically.
7. Review and verify the revised head when risk warrants another pass. For stacked PRs, reconcile their bases and test the combined result before merging. Do not carry an earlier clean review forward across unreviewed changes.
8. Merge when authorized and ready. Close the issue when its acceptance criteria are satisfied; keep outstanding acceptance work open or link a clearly scoped follow-up. A merged collector with synthetic tests does not establish live-provider compatibility, and a merge does not mean deployed.

The common path is therefore **Randall directs -> Codex implements and verifies -> Claude reviews -> Codex evaluates and remediates -> Randall accepts**. Gemini joins before or during that loop when investigation is likely to change the plan, and may arbitrate a disputed technical assumption. Routine, low-risk changes do not require every agent or repeated review. Additional inference-time work should buy down meaningful uncertainty, not become ceremony.

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

## Planned direction: evidence-based routing

Workbench does not currently assign tasks to models, run the review loop automatically, or learn which provider should receive a task. Those are possible future capabilities, not descriptions of the present system.

The planned direction is to record enough structured evidence to compare workflows on real work: task type, assigned role and provider, plan, implementation and test results, review findings, findings accepted or rejected, human corrections, elapsed time, cost where available, and final outcome. That history could support recommendations such as using one model for Terraform implementation, another for security review, and another for repository-wide investigation.

Later routing may adapt between tasks or in real time as evidence changes. A task could begin with the default route, add an investigator after unexpected cross-system effects appear, switch implementers after repeated failed checks, or request an independent review when risk rises. Any such behavior should remain observable, bounded by explicit policy and budget, and subordinate to human authority. Provider reputation or model self-confidence is not enough; routing changes should be explainable from task evidence and reversible by the operator.
