---
name: aiw-flow-handoff
description: Prepare a copyable FLOW handoff or review packet for a chosen role and exact source revision. Use for a requested handoff/review, not for automatically sending work to another agent.
---

# Prepare a FLOW handoff or review

Select the recipient role from the user's request; roles are assignments, not provider rankings. Use `flow_packet` with `kind=handoff`, the role, exact expected source heads, selected PR links, scope, test results, unresolved findings, and limitations. The CLI equivalent is `ai-workbench work packet ISSUE --kind handoff --role ROLE` with corresponding repeatable evidence flags. These supplied claims are marked unverified; inspect the selected issue, source branch/head, and relevant repository instructions before relying on them.

Make the packet copyable: identify the issue and PR, exact branch/head, what changed, checks and results, open findings, missing evidence, and the next authorized action. Distinguish fixture checks, reviewer judgment, merged status, and deployment. A new commit makes a review of the earlier head stale. If the packet marks a head stale or unknown, ask for a fresh review of the current revision.

Sanitize caller-supplied evidence; omit credentials, private paths, raw prompts, transcripts, and unrelated workspaces. Packet creation neither starts an agent nor sends a message. A send request needs an existing separately authorized delivery mechanism. Stop after preparing the selected handoff.
