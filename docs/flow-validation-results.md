# FLOW synthetic validation checkpoint

Tested repository commit: `517d0d9` (after PR #147), 2026-09-26. Tools: Python 3.12.14, Git 2.51.1, uv 0.12.18. This is a synthetic local check, not a live GitHub/Jira compatibility claim or production pilot.

- Focused FLOW suite (journey, tasks, history, publication, source, relations, dashboard, migration, MCP, packets, code workspaces): **63 passed**. `git diff --check`: passed.
- Full `uv run pytest -q`: **490 passed, 20 skipped, 24 failed**. The failing groups were the same ones seen before the FLOW integrations in this workspace: 1 Cloudflare client test due to an injected SOCKS proxy without `socksio`; 18 Docker worker fixture tests with a profile missing the required `user`; 4 reboot subcases needing absent Claude/Antigravity binaries; and 1 Unix socket test denied by this sandbox. These are genuine gate failures here, not reported as passes or skips. Investigate/fix them before satisfying #118's full-suite criterion.
- No live provider call, agent launch, tracker write, deployment, production migration, or background collector activation was performed. GitHub/Jira adapter tests used synthetic records and caller-injected transport; they do not prove compatibility with a live account.

The small useful release is local capture, exact Git checkpoints, explicit task outcome history, resume/handoff packets, selected source status, optional publication, and read-only reporting. Keep optional collection, automatic task selection/execution, and direct tracker publishing deferred. A user can disable publication while retaining local metadata and accepted actions. The pilot and recovery steps are in [flow-pilot-and-recovery.md](flow-pilot-and-recovery.md).

Remaining #118 validation: clear or explicitly gate the full-suite failures, exercise a deliberate non-sensitive live pilot if authorized, and extend fault injection for interrupted multi-row migration and profile/URL edge cases. An interrupted import can leave some rows checkpointed; reconcile its operation history before retrying rather than assuming an all-or-nothing transaction.

## Follow-up validation after portable fixture repair

At commit `fb585a4` plus the #118 migration recovery tests: `uv run pytest -q` completed with **506 passed, 26 skipped, 4 passing subtests, and 1 deprecation warning**; `git diff --check` passed. The six new environment skips relative to the earlier gate are five artifact-path ordering cases preempted by the product's root-caller rejection, plus a Unix socket case in a sandbox that denies socket creation. Explicit live Docker cases remain opt-in skips. No production validation policy was weakened. A two-row import interruption test now confirms that a fresh preview and replay of stable operation IDs preserves the first checkpoint and adds the second only once.
