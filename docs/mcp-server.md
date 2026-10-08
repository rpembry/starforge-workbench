# Local MCP server

`wb-mcp` exposes a small stdio-only MCP tool surface for a local MCP-capable
agent. It never opens a listener. It uses the same private `WB_CLIENT_CONFIG`
operator configuration as `wb-api`; keep that file outside Git and mode `0600`.

Set `WB_MANIFEST` to the absolute, non-symlink path of your private Workbench
manifest, then configure the MCP host to run `uv run wb-mcp` from this checkout.
The central API tools read bounded Workbench records, create only proposals or
attributed accomplishments, and return a standup report. They cannot accept or
complete central actions. Optional local FLOW tools below can change local
task documents when enabled; none executes a task autonomously.

`workbench_capabilities` reports only this running stdio server's implemented
tool families and package version. Discover exact schemas through the MCP host.
It does not establish client-side skill discovery or API health. See
[agent-interface.md](agent-interface.md) for interface routing and current gaps.

The read-only `registered_session_list`, `registered_session_detail`,
`registered_session_instructions`, and `registered_instruction_status` tools
use the existing operator API. They bound pages to 50 records, require exact
opaque IDs, and report API check time separately from collector timestamps and
visibility. Their output omits host identifiers, instruction text, preview
excerpts, idempotency keys, leases, and credentials. API failure is a tool
error, not evidence that a session is offline. These tools do not queue or
retry instructions, attach to a provider, or alter delivery state.

`list_agent_roster` reads the private mode-0600 specialist roster selected by
`WB_MCP_AGENT_ROSTER` or the default local roster path; it returns role
descriptions without prompts or configuration paths. `agent_handoff_preview`
resolves an OpenCode context through the protected
collector registration state and current operator API evidence. Configure
`WB_MCP_REGISTRATION_STATE` and `WB_MCP_LAUNCHER_STATE` privately to enable
generation verification. It returns an exact opaque registration ID only for a
fresh, verified OpenCode session. A listed, enabled daemon-backed Codex context
is resolved through its exact protected launcher binding and local app-server
idle status instead. `agent_handoff_send` also requires
`WB_MCP_ALLOW_HANDOFF=1`, an explicit operator request, that exact ID, bounded
plain text, and a stable caller-generated idempotency key. OpenCode uses the
existing instruction queue; `accepted` means queued, not provider receipt or
completed work. After an uncertain OpenCode result, retry with the **same key
and text** to inspect the original attempt. Codex uses a local private journal
and app-server `turn/start`; `accepted` means the daemon acknowledged one turn,
not that work finished. An uncertain Codex request is never automatically
resent; inspect the exact target before making another request. A changed
target or unsupported provider returns a copyable handoff instead of sending.
These tools never start, resume, or rebind a provider. Enable the send tool
only in the coordinator's private MCP configuration; read-only preview and
roster listing do not authorize delivery.

The server instructions tell connected agents to treat an unsupported Workbench
(`aiw`) operation as a possible MCP capability gap. An agent should consider
creating a GitHub feature request for future support; if it lacks GitHub access,
it should notify the user of the gap and suggest the feature request instead.
Filing or suggesting an issue does not replace completing the current request
through an authorized, safe path when one exists.

`restore_session` defaults to a launcher preview. A live restoration requires
both an explicit `dry_run: false` tool argument and `WB_MCP_ALLOW_RESTORE=1` in
the server environment. It targets only a context ID from the private
manifest and launches headlessly; it never accepts a command, working directory,
provider argument, or terminal target from the MCP caller.

## Optional local FLOW tools

Set `WB_MCP_FLOW_PROFILE` in the **server process environment** to the absolute
path of one private FLOW profile to expose read-only `flow_*` tools. The client
cannot select a profile or filesystem root. Reads include bounded work-item
pages, task views/history, an operation-status lookup, and a local resume
summary or handoff packet through `flow_packet`. `next` only suggests work. Raw document blocks, full context files,
and private code paths are not returned by default. A configured profile is an
intentional disclosure to the connected model; choose the MCP host and model
accordingly. `WB_MCP_FLOW_DISCLOSE_PATHS=1` deliberately includes configured
code-workspace paths in resume/prepare results.

Set `WB_MCP_FLOW_WRITE=1` separately to register work-item creation and task
edit tools. A task edit requires its current revision, document hash and stable
operation ID; `flow_task_preview` returns a bounded diff and content hash plus a
server-local token (lost on restart and expired after 30 minutes if unused).
`flow_task_apply` accepts only that token, uses the same domain operation as the
CLI, and preserves its checkpoint/recovery behavior. Committed tokens leave the
pending-preview limit; a bounded in-process cache permits short-term replay of
the result. After an uncertain result, inspect `flow_operation_status` and the
current task state before retrying. The token is not proof of human review;
write requests still need actual authorization. `WB_MCP_FLOW_PREPARE=1` in
addition registers explicit local code-workspace preparation, including a
read-only remote clone only when the private binding already permits it.

For task `add` and `update`, `flow_task_preview` also accepts `blocked_by` (a
list of same-item task IDs) and `parent_task_id` (an active same-item task ID).
Pass `blocked_by: []` to clear dependencies or `clear_parent: true` to remove
the organizational parent on update. Parentage does not gate `next`; explicit
dependencies do. Task reads and packets expose both relationships. No parent
or child is automatically completed.

These flags do not configure a Codex client, initialize a FLOW profile, launch
a provider, link an action, publish to the API, or change a source tracker.
Central action linking/publication belongs to #113; source enrichment to #114.
If MCP is missing, the documented `ai-workbench work` and `tasks` CLI use the
same local domain operations for an already authorized request. A denied MCP
write, unknown profile, stale preview, or uncertain result is not a reason to
use the CLI as a bypass. No current client configuration is rewritten.
