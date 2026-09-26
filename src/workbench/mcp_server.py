"""Local stdio MCP server for the authenticated Starforge Workbench API."""
import os
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Literal
from uuid import uuid4

from mcp.server.mcpserver import MCPServer

from .client import client
from .flow_mcp import register_flow_tools

TABLES = frozenset({'actions', 'artifacts', 'collectors', 'events', 'import_batches',
                    'import_records', 'objectives', 'runs'})


def _release_version() -> str:
    try:
        return version('starforge-ai-workbench')
    except PackageNotFoundError:
        return 'unknown'


def _manifest_path() -> Path:
    value = os.environ.get('WB_MANIFEST')
    if not value:
        raise ValueError('WB_MANIFEST must name the private Workbench manifest')
    path = Path(value).expanduser()
    if not path.is_absolute() or path.is_symlink():
        raise ValueError('WB_MANIFEST must be an absolute, non-symlink path')
    return path


def _contexts(path: Path) -> list[dict[str, object]]:
    """Return display-safe context data without provider command or path fields."""
    import yaml
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict) or not isinstance(data.get('contexts'), list):
        raise ValueError('Workbench manifest must contain a contexts list')
    result = []
    for item in data['contexts']:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str):
            raise ValueError('Each Workbench context must have an id')
        result.append(
            {key: item[key] for key in ('id', 'title', 'provider', 'enabled') if key in item}
        )
    return result


def build_server(api_factory=client, manifest_path=_manifest_path, context_loader=_contexts,
                 restore=None, flow_profile=None, flow_write=None,
                 flow_prepare=None, flow_disclose_paths=None) -> MCPServer:
    """Build a server with injectable dependencies for isolated tests."""
    selected_flow_profile = flow_profile or os.environ.get('WB_MCP_FLOW_PROFILE')
    selected_flow_write = flow_write if flow_write is not None else os.environ.get('WB_MCP_FLOW_WRITE') == '1'
    selected_flow_prepare = flow_prepare if flow_prepare is not None else os.environ.get('WB_MCP_FLOW_PREPARE') == '1'
    selected_flow_paths = flow_disclose_paths if flow_disclose_paths is not None else os.environ.get('WB_MCP_FLOW_DISCLOSE_PATHS') == '1'
    server = MCPServer(name='starforge-workbench', title='Starforge Workbench',
                       description='Local tools for Workbench state and deliberate launcher restoration.',
                       instructions=('This local stdio server uses the configured Workbench operator credential. '
                                     'Observations and proposals do not prove task completion. Session restoration '
                                     'is disabled by default and requires an explicit local opt-in. Prefer an available, '
                                     'correctly scoped typed tool. A denied operation, stale revision, or uncertain write '
                                     'must not be retried through a shell or another interface without reconciliation. '
                                     'A documented CLI equivalent is useful when MCP is unavailable and the request is '
                                     'authorized. When a requested '
                                     'Starforge Workbench (aiw) operation is not directly supported by this MCP '
                                     'server, consider creating a GitHub feature request to add the capability. If '
                                     'GitHub access is unavailable, notify the user of the capability gap and suggest '
                                     'the feature request. Filing or suggesting an issue does not replace completing '
                                     'the current request through an authorized, safe path when one exists.'),
                       version='0.2.0')

    def request(method: str, path: str, payload=None):
        with api_factory(role='operator') as api:
            response = api.request(method, path, json=payload)
            response.raise_for_status()
            return response.json()

    @server.tool(name='workbench_capabilities', structured_output=True)
    def workbench_capabilities() -> dict[str, object]:
        """Report this running stdio server's release and implemented tool families; no profile, client-installation, or remote-service claim."""
        return {
            'server_version': _release_version(),
            'transport': 'stdio',
            'evidence': 'running-server',
            'tool_families': ['configured-contexts', 'browser-desired-state',
                              'bounded-worklog', 'standup', 'session-restore-preview',
                              *(['opt-in-session-restore'] if os.environ.get('WB_MCP_ALLOW_RESTORE') == '1' else [])],
            'flow': ('local-read-write' if selected_flow_profile and selected_flow_write else
                     'local-read' if selected_flow_profile else 'unavailable'),
            'client_skill_discovery': 'unknown',
            'api_health': 'not_checked',
        }

    @server.tool(name='list_contexts', structured_output=True)
    def list_contexts() -> list[dict[str, object]]:
        """List configured launcher contexts without disclosing commands or filesystem paths."""
        return context_loader(manifest_path())

    @server.tool(name='browser_workspace_list', structured_output=True)
    def browser_workspace_list() -> dict[str, object]:
        """List Workbench-owned Chrome workspaces and their named URL entries."""
        return request('GET', '/api/browser/workspaces')

    @server.tool(name='browser_workspace_add', structured_output=True)
    def browser_workspace_add(name: str, url: str, workspace: str = 'default', match: Literal['origin', 'url'] = 'origin') -> dict[str, object]:
        """Add a named URL to a Chrome workspace; use this for requests such as adding a site to Chrome."""
        return request('POST', f'/api/browser/workspaces/{workspace}/entries',
                       {'name': name, 'url': url, 'match': match})

    @server.tool(name='browser_workspace_remove', structured_output=True)
    def browser_workspace_remove(name: str, workspace: str = 'default') -> dict[str, object]:
        """Remove a named URL from a Chrome workspace; this changes desired state but does not close tabs."""
        return request('DELETE', f'/api/browser/workspaces/{workspace}/entries/{name}')

    @server.tool(name='worklog_query', structured_output=True)
    def worklog_query(table: Literal['actions', 'artifacts', 'collectors', 'events', 'import_batches', 'import_records', 'objectives', 'runs'], limit: int = 100, offset: int = 0) -> dict[str, object]:
        """Read a bounded page of current Workbench records through its authenticated API."""
        if table not in TABLES or not 1 <= limit <= 500 or offset < 0:
            raise ValueError('Use an allowed table, limit 1..500, and a non-negative offset')
        return request('GET', f'/api/{table}?limit={limit}&offset={offset}')

    @server.tool(name='worklog_append', structured_output=True)
    def worklog_append(kind: Literal['proposal', 'accomplishment'], summary: str, details: str = '', project: str | None = None) -> dict[str, object]:
        """Record a proposal or attributed accomplishment; never accepts or completes an action."""
        source_id = f'mcp-{uuid4()}'
        if kind == 'proposal':
            return request('POST', '/api/actions', {'title': summary, 'details': details, 'project': project, 'status': 'proposed', 'source': 'mcp', 'source_id': source_id})
        return request('POST', '/api/events', {'kind': 'accomplishment', 'summary': summary, 'details': details, 'project': project, 'source': 'mcp', 'source_id': source_id})

    @server.tool(name='standup_report', structured_output=True)
    def standup_report() -> dict[str, object]:
        """Return the current read-only standup report from the Workbench API."""
        return request('GET', '/api/reports/standup')

    @server.tool(name='restore_session', structured_output=True)
    def restore_session(context: str, dry_run: bool = True) -> dict[str, object]:
        """Preview or, after local opt-in, restore one named configured launcher context."""
        contexts = context_loader(manifest_path())
        if context not in {item['id'] for item in contexts}:
            return {'context': context, 'dry_run': dry_run, 'status': 'denied', 'message': 'Unknown Workbench context'}
        if not dry_run and os.environ.get('WB_MCP_ALLOW_RESTORE') != '1':
            return {'context': context, 'dry_run': dry_run, 'status': 'denied',
                    'message': 'Set WB_MCP_ALLOW_RESTORE=1 for a deliberate live restoration'}
        if restore is None:
            from starforge_workbench.cli import main as launcher
            args = ['--manifest', str(manifest_path())]
            if dry_run:
                args.append('--dry-run')
            launcher([*args, 'up', context, '--headless'])
        else:
            restore(context, dry_run)
        return {'context': context, 'dry_run': dry_run, 'status': 'previewed' if dry_run else 'restore_requested'}

    if selected_flow_profile:
        register_flow_tools(server, profile=selected_flow_profile, allow_write=selected_flow_write,
                            allow_prepare=selected_flow_write and selected_flow_prepare,
                            disclose_paths=selected_flow_paths)
    return server


mcp = build_server()


def main() -> None:
    mcp.run(transport='stdio')
