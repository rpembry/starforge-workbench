"""Local stdio MCP server for the authenticated Starforge Workbench API."""
import os
import re
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Literal
from uuid import uuid4

from mcp.server.mcpserver import MCPServer

from .client import client
from .flow_mcp import register_flow_tools
from .session_views import REASON_LABELS

TABLES = frozenset({'actions', 'artifacts', 'collectors', 'events', 'import_batches',
                    'import_records', 'objectives', 'runs'})
SESSION_ID = re.compile(r'^[A-Za-z0-9_-]{16,128}$')
INSTRUCTION_ID = re.compile(r'^[A-Za-z0-9_-]{16,128}$')


def _safe_session(row):
    fields = ('id', 'visibility', 'evidence_state',
              'observed_at', 'heartbeat_at', 'last_activity_at')
    result = {key: row.get(key) for key in fields}
    result['provider'] = row.get('provider') if row.get('provider') in {
        'opencode', 'codex', 'claude', 'ollama', 'antigravity'} else 'other'
    result['reason'] = row.get('reason') if row.get('reason') in REASON_LABELS else 'unknown'
    result['send_eligible'] = (row.get('visibility') == 'fresh' and row.get('provider') == 'opencode'
                               and row.get('evidence_state') not in {'unknown', 'stopped'})
    return result


def _safe_instruction(row, history=False):
    fields = ('id', 'registered_session_id', 'state', 'reason_code', 'created_at', 'updated_at',
              'expires_at', 'received_at', 'responded_at', 'terminal_at')
    result = {key: row.get(key) for key in fields}
    if history:
        result['history'] = [{key: event.get(key) for key in ('state', 'reason_code', 'occurred_at')}
                             for event in row.get('history', [])[:50]]
    return result


def _exact_id(value, pattern, label):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f'{label} must be an exact opaque ID')
    return value


def _page(limit, offset):
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50 or not isinstance(offset, int) or isinstance(offset, bool) or not 0 <= offset <= 10000:
        raise ValueError('Use limit 1..50 and offset 0..10000')


def _checked_at():
    return datetime.now(timezone.utc).isoformat()


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
                 flow_prepare=None, flow_disclose_paths=None, title_service=None) -> MCPServer:
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
                              'bounded-worklog', 'standup', 'registered-session-status',
                              'session-restore-preview', 'codex-title-preview-status',
                              *(['opt-in-codex-title-apply'] if os.environ.get('WB_MCP_ALLOW_TITLE_APPLY') == '1' else []),
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

    def reconcile():
        if title_service is not None:
            return title_service
        from starforge_workbench.cli import title_reconciler
        return title_reconciler(manifest_path())

    @server.tool(name='codex_title_preview', structured_output=True)
    def codex_title_preview(context_ids: list[str]) -> dict[str, object]:
        """Preview exact configured Codex context bindings and desired titles; read-only for provider state."""
        if not context_ids or len(context_ids) > 50 or len(context_ids) != len(set(context_ids)):
            raise ValueError('Select 1..50 distinct exact context IDs')
        allowed = {item['id'] for item in context_loader(manifest_path()) if item.get('provider') == 'codex'}
        if any(identity not in allowed for identity in context_ids):
            raise ValueError('Select configured Codex context IDs')
        return reconcile().preview(context_ids)

    @server.tool(name='codex_title_status', structured_output=True)
    def codex_title_status(plan_id: str) -> dict[str, object]:
        """Read a private reconciliation plan and its per-row outcomes by exact plan ID."""
        return reconcile().status(plan_id)

    @server.tool(name='codex_title_apply', structured_output=True)
    def codex_title_apply(plan_id: str, context_ids: list[str] | None = None,
                          all_eligible: bool = False, mode: Literal['strict', 'practical'] = 'strict',
                          confirm_non_atomic: bool = False) -> dict[str, object]:
        """Apply exact preview rows; practical mode needs explicit acknowledgement of its title race."""
        if os.environ.get('WB_MCP_ALLOW_TITLE_APPLY') != '1':
            return {'plan_id': plan_id, 'status': 'denied', 'reason': 'WB_MCP_ALLOW_TITLE_APPLY is not enabled'}
        return reconcile().apply(plan_id, selected=context_ids, all_eligible=all_eligible,
                                 mode=mode, confirm_non_atomic=confirm_non_atomic)

    @server.tool(name='codex_title_undo', structured_output=True)
    def codex_title_undo(plan_id: str, context_id: str, mode: Literal['strict', 'practical'] = 'strict',
                         confirm_non_atomic: bool = False) -> dict[str, object]:
        """Restore one verified prior title; practical mode acknowledges the non-atomic title race."""
        if os.environ.get('WB_MCP_ALLOW_TITLE_APPLY') != '1':
            return {'plan_id': plan_id, 'status': 'denied', 'reason': 'WB_MCP_ALLOW_TITLE_APPLY is not enabled'}
        return reconcile().undo(plan_id, context_id, mode=mode,
                                confirm_non_atomic=confirm_non_atomic)

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

    @server.tool(name='registered_session_list', structured_output=True)
    def registered_session_list(limit: int = 25, offset: int = 0) -> dict[str, object]:
        """Read a bounded page of registered-session evidence without host identifiers or provider content."""
        _page(limit, offset)
        response = request('GET', f'/api/registered-sessions?limit={limit}&offset={offset}')
        return {'items': [_safe_session(row) for row in response['items'][:limit]],
                'limit': limit, 'offset': offset, 'checked_at': _checked_at(), 'api_status': 'available'}

    @server.tool(name='registered_session_detail', structured_output=True)
    def registered_session_detail(session_id: str) -> dict[str, object]:
        """Read one exact registered-session status, freshness, and send eligibility."""
        _exact_id(session_id, SESSION_ID, 'session_id')
        return {'session': _safe_session(request('GET', f'/api/registered-sessions/{session_id}')),
                'checked_at': _checked_at(), 'api_status': 'available'}

    @server.tool(name='registered_session_instructions', structured_output=True)
    def registered_session_instructions(session_id: str, limit: int = 20, offset: int = 0) -> dict[str, object]:
        """Read bounded instruction delivery states for one exact session; no bodies or leases."""
        _exact_id(session_id, SESSION_ID, 'session_id')
        _page(limit, offset)
        response = request('GET', f'/api/instructions?limit={limit}&offset={offset}&registered_session_id={session_id}')
        if any(row.get('registered_session_id') != session_id for row in response['items']):
            raise ValueError('API returned an unrelated session instruction')
        return {'items': [_safe_instruction(row) for row in response['items'][:limit]], 'session_id': session_id,
                'limit': limit, 'offset': offset, 'checked_at': _checked_at(), 'api_status': 'available'}

    @server.tool(name='registered_instruction_status', structured_output=True)
    def registered_instruction_status(session_id: str, instruction_id: str) -> dict[str, object]:
        """Read one instruction's state and bounded timeline for its exact registered session."""
        _exact_id(session_id, SESSION_ID, 'session_id')
        _exact_id(instruction_id, INSTRUCTION_ID, 'instruction_id')
        row = request('GET', f'/api/instructions/{instruction_id}')
        if row.get('registered_session_id') != session_id:
            raise ValueError('Instruction does not belong to the selected session')
        return {'instruction': _safe_instruction(row, history=True),
                'checked_at': _checked_at(), 'api_status': 'available'}

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
