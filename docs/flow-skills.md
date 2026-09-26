# FLOW workflow skills

The source collection under `skills/` contains four provider-neutral Agent Skills:
`aiw-flow-capture`, `aiw-flow-resume`, `aiw-flow-handoff`, and
`aiw-flow-closeout`. Each describes a user workflow over the same local FLOW
operations exposed by `ai-workbench work/tasks` and, when explicitly configured,
`wb-mcp`. Natural-language requests can select a relevant installed skill;
using the skill does not authorize a write. The portable instructions do not
depend on a particular provider, role ranking, or automatic agent handoff.

The deterministic `ai-workbench work packet ISSUE` command (or `flow_packet` MCP
tool) prepares a bounded **read-only** resume packet. Add `--kind handoff --role
ROLE` and selected `--expected-head NAME=SHA`, `--pr-link URL`, `--test TEXT`,
`--finding TEXT`, or `--limitation TEXT` values for a review packet. The output
records exact current Git heads, revision, generated time, omissions, and
whether expected heads still match. Caller-supplied links and test claims are
marked unverified. It does not read raw chats, start a provider, switch a live
working directory, publish an update, or send a handoff. Private repository
paths are withheld unless `--include-paths` is deliberately chosen; the MCP
server has its separate `WB_MCP_FLOW_DISCLOSE_PATHS` gate.

For a manual user-scoped Codex trial, copy only a selected skill directory from
a pinned checkout or release into the host's documented user skill directory
(currently `~/.agents/skills` for supported Codex versions). Check for a
same-name destination first: an existing directory may contain user edits and
must not be overwritten. Repeat installation is a no-op only when file bytes
match; otherwise review the difference. Remove only the exact copied skill
after checking for edits. A repository-scoped trial can use `.agents/skills`
inside a deliberately selected synthetic project. The shared preview/update/
remove tooling and clean-client discovery verification belong to #122. Merely
checking these source files into Workbench does not make them visible in an
already running tab, another host/container, or every subagent.

Fixture tests verify packet bounds, CLI/MCP parity on a temporary profile,
blocked work, caller-supplied evidence labels, and stale code heads. They do
not prove that a live Codex client discovered or followed a newly installed
skill. Record the client/server/skill versions and actual tool listing in a
fresh session before making that claim. Central action linking/publication
remains #113; source tracker enrichment remains #114.
