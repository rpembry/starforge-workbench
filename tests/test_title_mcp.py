"""Inspect registered typed MCP schema and invoke its actual handlers in isolation."""
import os
import uuid

from starforge_workbench.title_reconcile import Reconciler
from workbench.mcp_server import build_server

THREAD = str(uuid.UUID('11111111-1111-4111-8111-111111111111'))


class Catalog:
    guarded_rename = False
    def read(self, identity):
        return {'id': identity, 'title': '', 'cwd': '/synthetic/a', 'archived': False, 'source': 'cli'}


def test_registered_schema_and_cross_interface_blocked_state(tmp_path, monkeypatch):
    context = {'id': 'alpha', 'provider': 'codex', 'title': 'Alpha', 'cwd': '/synthetic/a'}
    binding = {'id': THREAD, 'provider': 'codex', 'cwd': '/synthetic/a'}
    service = Reconciler(lambda: [context], lambda _: binding, Catalog(), tmp_path / 'state')
    server = build_server(manifest_path=lambda: tmp_path / 'manifest',
                          context_loader=lambda _: [context], title_service=service)
    manager = server._tool_manager
    preview_tool = manager.get_tool('codex_title_preview')
    apply_tool = manager.get_tool('codex_title_apply')
    status_tool = manager.get_tool('codex_title_status')
    assert preview_tool.parameters['required'] == ['context_ids']
    assert {'plan_id', 'context_ids', 'all_eligible'} <= set(apply_tool.parameters['properties'])
    assert {'mode', 'confirm_non_atomic'} <= set(apply_tool.parameters['properties'])
    assert status_tool.parameters['required'] == ['plan_id']
    plan = preview_tool.fn(['alpha'])
    assert plan['rows'][0]['status'] == 'eligible'
    denied = apply_tool.fn(plan['plan_id'], ['alpha'])
    assert denied['status'] == 'denied'
    monkeypatch.setenv('WB_MCP_ALLOW_TITLE_APPLY', '1')
    blocked = apply_tool.fn(plan['plan_id'], ['alpha'])
    assert blocked['results']['alpha']['status'] == 'blocked'
    assert status_tool.fn(plan['plan_id'])['results'] == blocked['results']


def test_mcp_practical_confirmation_uses_same_plan_and_readback(tmp_path, monkeypatch):
    class Practical(Catalog):
        practical_rename = True
        def __init__(self): self.title = ''
        def read(self, identity):
            return {'id': identity, 'title': self.title, 'cwd': '/synthetic/a', 'archived': False, 'source': 'cli'}
        def rename_practical(self, identity, desired):
            self.title = desired
            return 'applied'
    context = {'id': 'alpha', 'provider': 'codex', 'title': 'Alpha', 'cwd': '/synthetic/a'}
    binding = {'id': THREAD, 'provider': 'codex', 'cwd': '/synthetic/a'}
    catalog = Practical()
    service = Reconciler(lambda: [context], lambda _: binding, catalog, tmp_path / 'state')
    server = build_server(manifest_path=lambda: tmp_path / 'manifest',
                          context_loader=lambda _: [context], title_service=service)
    tools = server._tool_manager
    plan = tools.get_tool('codex_title_preview').fn(['alpha'])
    monkeypatch.setenv('WB_MCP_ALLOW_TITLE_APPLY', '1')
    result = tools.get_tool('codex_title_apply').fn(plan['plan_id'], ['alpha'], False, 'practical', True)
    assert result['results']['alpha']['status'] == 'applied'
    assert service.status(plan['plan_id'])['verified_writes']['alpha']['mode'] == 'practical'
    assert catalog.title == 'Alpha'
