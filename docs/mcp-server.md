# Local MCP server

`wb-mcp` exposes a small stdio-only MCP tool surface for a local MCP-capable
agent. It never opens a listener. It uses the same private `WB_CLIENT_CONFIG`
operator configuration as `wb-api`; keep that file outside Git and mode `0600`.

Set `WB_MANIFEST` to the absolute, non-symlink path of your private Workbench
manifest, then configure the MCP host to run `uv run wb-mcp` from this checkout.
The tools read bounded Workbench records, create only proposals or attributed
accomplishments, and return a standup report. They cannot accept, complete, or
execute work.

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
