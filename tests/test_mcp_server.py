import asyncio

from mcp import Client
from workbench.mcp_server import build_server


class Response:
    def __init__(self, value): self.value = value
    def raise_for_status(self): return None
    def json(self): return self.value


class API:
    def __init__(self): self.calls = []
    def __enter__(self): return self
    def __exit__(self, *args): return None
    def request(self, method, path, json=None):
        self.calls.append((method, path, json))
        return Response({'method': method, 'path': path, 'payload': json})


def invoke(server, name, arguments=None):
    async def run():
        async with Client(server) as connected:
            return await connected.call_tool(name, arguments or {})
    return asyncio.run(run()).structured_content


def test_read_and_append_tools_use_bounded_api_contracts():
    api = API()
    server = build_server(api_factory=lambda **kwargs: api, manifest_path=lambda: None, context_loader=lambda path: [{'id': 'daily', 'provider': 'codex', 'enabled': True}])
    assert invoke(server, 'worklog_query', {'table': 'actions', 'limit': 2})['path'] == '/api/actions?limit=2&offset=0'
    assert invoke(server, 'worklog_append', {'kind': 'proposal', 'summary': 'Review tests'})['payload']['status'] == 'proposed'
    assert invoke(server, 'worklog_append', {'kind': 'accomplishment', 'summary': 'Tests passed'})['payload']['kind'] == 'accomplishment'
    assert invoke(server, 'standup_report')['path'] == '/api/reports/standup'


def test_context_tools_default_to_preview_and_reject_live_restore():
    restored = []
    server = build_server(api_factory=lambda **kwargs: API(), manifest_path=lambda: None, context_loader=lambda path: [{'id': 'daily', 'provider': 'codex', 'enabled': True}], restore=lambda context, dry_run: restored.append((context, dry_run)))
    assert invoke(server, 'list_contexts') == {'result': [{'id': 'daily', 'provider': 'codex', 'enabled': True}]}
    assert invoke(server, 'restore_session', {'context': 'daily'})['status'] == 'previewed'
    assert restored == [('daily', True)]
    denied = invoke(server, 'restore_session', {'context': 'daily', 'dry_run': False})
    assert denied['status'] == 'denied'
    assert 'WB_MCP_ALLOW_RESTORE' in denied['message']
