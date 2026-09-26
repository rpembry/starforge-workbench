# Install and inspect Workbench skills

Workbench keeps one portable skill source collection in `skills/`. To use a
selected skill outside this repository, install from an **exact, committed
local checkout**. This operation changes only the selected skill directory;
it does not edit Codex configuration, connect MCP, enable FLOW writes, start a
provider, or initialize a workspace.

```sh
ai-workbench skills preview aiw-flow-resume --source /path/to/pinned-checkout
ai-workbench skills install aiw-flow-resume --source /path/to/pinned-checkout
ai-workbench skills doctor --source /path/to/pinned-checkout
```

The default is user scope, `$HOME/.agents/skills`. Use `--scope repo --repo
/path/to/synthetic-project` only for a deliberately selected Git repository.
`preview` and `doctor` are read-only. `install` refuses an existing unowned
name; repeating an identical managed install is a no-op. To move a managed
skill to a newer committed source, preview then use `update`. Modified managed
files, extra files, symlinked paths, and interrupted replacements require
manual reconciliation rather than overwrite. `remove` deletes only an unchanged
managed copy by exact name; it does not remove unrelated skills. Check both
scopes with `doctor` to find duplicate names before starting a fresh client.

The receipt inside an installed directory records its source commit and file
hashes. It is local installer metadata, not permission for an agent to act.
The selected checkout must have committed, clean skill files. This workflow
does not pull a branch or execute skill scripts during installation. A skill
may be discovered implicitly or invoked explicitly after the client loads it;
discovery itself is not authorization for a state change.

[Official OpenAI Docs for Codex skills](https://learn.chatgpt.com/docs/build-skills)
describe user and repository `.agents/skills` locations and warn that duplicate
names are not merged. They also describe plugin packaging for reusable skill
bundles. A plugin could eventually bundle Workbench's skills and MCP connection,
but the local stdio server needs deliberate private client configuration and
profile/write gates; this first path keeps those choices separate. The
[official MCP guide](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
documents explicit Codex stdio configuration and tool policies. Configure
`wb-mcp` only after a separate request, preserving existing allowlists,
approval settings, host profile, and credentials. Do not infer that a generic
shell tool may bypass a denied Workbench operation.

`doctor` reports the installed source commit, on-disk scope, duplicate names,
and whether a `wb-mcp` entry appears enabled in the selected user's Codex
configuration. It does not print credential values, contact the API, start MCP,
or prove which tools and skills a running client has loaded. Its client version
probe is optional (`--probe-client-version`). A fresh Codex session in a
different synthetic repository is needed to verify actual discovery, tool
listing, and allowed behavior. Existing tabs, remote hosts, containers, and
subagents may differ; record their evidence separately. The default automated
tests use temporary homes and fake Git checkouts, not a live agent.
