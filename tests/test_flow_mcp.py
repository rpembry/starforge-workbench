import asyncio
import subprocess

import pytest
from mcp import Client

from starforge_workbench.flow import init_profile, open_item
from starforge_workbench.flow_tasks import inspect, mutate
from workbench import flow_mcp
from workbench.mcp_server import build_server


SOURCE = 'https://github.com/example-org/example-repo/issues/42'


def call(server, name, arguments=None):
    async def run():
        async with Client(server) as connected:
            return await connected.call_tool(name, arguments or {})
    return asyncio.run(run())


def names(server):
    async def run():
        async with Client(server) as connected:
            return {tool.name for tool in (await connected.list_tools()).tools}
    return asyncio.run(run())


@pytest.fixture
def profile(tmp_path, monkeypatch):
    monkeypatch.setattr('starforge_workbench.flow._inside_checkout', lambda root: False)
    selected, root = tmp_path / 'private' / 'profile.json', tmp_path / 'metadata'
    init_profile(root, profile=selected)
    subprocess.run(['git', '-C', str(root), 'config', 'user.name', 'Example Operator'], check=True)
    subprocess.run(['git', '-C', str(root), 'config', 'user.email', 'operator@example.com'], check=True)
    open_item(SOURCE, profile=selected)
    return selected


def test_flow_tools_are_opt_in_and_reads_do_not_mutate(profile):
    profile.with_suffix('.lock').unlink()
    assert 'flow_items' not in names(build_server(flow_profile=''))
    server = build_server(flow_profile=profile)
    assert 'flow_items' in names(server)
    assert 'flow_task_preview' not in names(server)
    before = inspect(SOURCE, 'list', profile=profile)
    assert call(server, 'flow_items').structured_content['total'] == 1
    assert call(server, 'flow_item_show', {'reference': SOURCE}).structured_content['work_item_id']
    assert call(server, 'flow_task_read', {'reference': SOURCE, 'view': 'next'}).structured_content['suggestion'] is None
    context = call(server, 'flow_resume_context', {'reference': SOURCE}).structured_content
    assert context['repository_bindings'] == 'none'
    assert context['agent_session'] == 'not started'
    assert not profile.with_suffix('.lock').exists()
    assert not profile.with_suffix('.code.json').exists()
    assert inspect(SOURCE, 'list', profile=profile) == before
    assert call(server, 'workbench_capabilities').structured_content['flow'] == 'local-read'


def test_preview_apply_and_cli_use_same_checkpoint_and_replay(profile):
    server = build_server(flow_profile=profile, flow_write=True)
    assert 'flow_prepare_workspace' not in names(server)
    state = inspect(SOURCE, 'list', profile=profile)
    args = {'reference': SOURCE, 'command': 'add', 'operation_id': 'mcp-add-01',
            'expected_revision': state['revision'], 'expected_hash': state['document_hash'],
            'title': 'Review synthetic fixture', 'acceptance': 'Tests pass'}
    preview = call(server, 'flow_task_preview', args).structured_content
    assert preview['status'] == 'preview'
    assert 'Review synthetic fixture' in preview['change_preview']
    assert 'proposed' not in preview
    applied = call(server, 'flow_task_apply', {'preview_token': preview['preview_token']}).structured_content
    assert applied['status'] == 'committed'
    assert applied['publication'] == 'pending'
    assert call(server, 'flow_task_apply', {'preview_token': preview['preview_token']}).structured_content == applied
    assert mutate(SOURCE, 'add', profile=profile, title='Review synthetic fixture',
                  acceptance='Tests pass', actor='MCP caller (authorization not independently verified)',
                  operation_id='mcp-add-01', expected_revision=state['revision'],
                  expected_hash=state['document_hash']) == applied
    assert call(server, 'flow_operation_status', {'reference': SOURCE,
                'operation_id': 'mcp-add-01'}).structured_content['status'] == 'committed'
    assert inspect(SOURCE, 'list', profile=profile)['tasks'][0]['task_id'] == applied['task_id']


def test_stale_preview_oversized_text_and_wrong_profile_are_denied(profile, tmp_path):
    server = build_server(flow_profile=profile, flow_write=True)
    state = inspect(SOURCE, 'list', profile=profile)
    base = {'reference': SOURCE, 'command': 'add', 'expected_revision': state['revision'],
            'expected_hash': state['document_hash'], 'title': 'One', 'operation_id': 'one-01'}
    preview = call(server, 'flow_task_preview', base).structured_content
    mutate(SOURCE, 'add', profile=profile, title='Two', operation_id='other-01')
    assert call(server, 'flow_task_apply', {'preview_token': preview['preview_token']}).is_error
    assert call(server, 'flow_task_apply', {'preview_token': 'not-a-token'}).is_error
    assert call(server, 'flow_task_preview', {**base, 'title': 'x' * 1001}).is_error
    assert call(server, 'flow_item_show', {'reference': '../../outside'}).is_error
    foreign = build_server(flow_profile=tmp_path / 'missing.json', flow_write=True)
    assert call(foreign, 'flow_items').is_error


def test_resume_only_handles_typed_missing_binding(profile, monkeypatch):
    server = build_server(flow_profile=profile)
    assert call(server, 'flow_resume_context', {'reference': SOURCE}).structured_content[
        'repository_bindings'] == 'none'

    def unrelated_error(*args, **kwargs):
        raise ValueError('No explicit repository binding; use work bind before preparation')

    monkeypatch.setattr(flow_mcp, 'workspace', unrelated_error)
    assert call(server, 'flow_resume_context', {'reference': SOURCE}).is_error


def test_committed_previews_do_not_exhaust_pending_capacity(profile, monkeypatch):
    monkeypatch.setattr(flow_mcp, 'PENDING_PREVIEW_LIMIT', 1)
    monkeypatch.setattr(flow_mcp, 'COMPLETED_REPLAY_LIMIT', 1)
    server = build_server(flow_profile=profile, flow_write=True)
    state = inspect(SOURCE, 'list', profile=profile)
    first_args = {'reference': SOURCE, 'command': 'add', 'operation_id': 'first-preview',
                  'expected_revision': state['revision'], 'expected_hash': state['document_hash'],
                  'title': 'First synthetic task'}
    first = call(server, 'flow_task_preview', first_args).structured_content
    assert call(server, 'flow_task_preview', {**first_args, 'operation_id': 'blocked-preview'}).is_error
    committed = call(server, 'flow_task_apply', {'preview_token': first['preview_token']}).structured_content
    assert committed['status'] == 'committed'
    assert call(server, 'flow_task_apply', {'preview_token': first['preview_token']}).structured_content == committed

    state = inspect(SOURCE, 'list', profile=profile)
    second = call(server, 'flow_task_preview', {**first_args, 'operation_id': 'second-preview',
        'expected_revision': state['revision'], 'expected_hash': state['document_hash'],
        'title': 'Second synthetic task'}).structured_content
    assert second['preview_token'] != first['preview_token']
    applied = call(server, 'flow_task_apply', {'preview_token': second['preview_token']}).structured_content
    assert applied['status'] == 'committed'
    assert call(server, 'flow_task_apply', {'preview_token': second['preview_token']}).structured_content == applied
    assert call(server, 'flow_task_apply', {'preview_token': first['preview_token']}).is_error


def test_abandoned_preview_expires_without_reaching_capacity(profile, monkeypatch):
    monkeypatch.setattr(flow_mcp, 'PENDING_PREVIEW_LIMIT', 1)
    clock = [0]
    monkeypatch.setattr(flow_mcp, 'monotonic', lambda: clock[0])
    server = build_server(flow_profile=profile, flow_write=True)
    state = inspect(SOURCE, 'list', profile=profile)
    args = {'reference': SOURCE, 'command': 'add', 'operation_id': 'abandoned-preview',
            'expected_revision': state['revision'], 'expected_hash': state['document_hash'],
            'title': 'Synthetic task'}
    abandoned = call(server, 'flow_task_preview', args).structured_content
    clock[0] = flow_mcp.PREVIEW_TTL_SECONDS + 1
    replacement = call(server, 'flow_task_preview', {**args, 'operation_id': 'fresh-preview'}).structured_content
    assert replacement['preview_token'] != abandoned['preview_token']
    assert call(server, 'flow_task_apply', {'preview_token': abandoned['preview_token']}).is_error
