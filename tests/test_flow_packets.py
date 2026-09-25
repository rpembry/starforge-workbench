import asyncio
import json
from pathlib import Path
import subprocess

import pytest
from mcp import Client

from starforge_workbench.flow import init_profile, main as work_main, open_item
from starforge_workbench.flow_code import bind, workspace
from starforge_workbench.flow_packets import build_packet
from starforge_workbench.flow_tasks import mutate
from workbench.mcp_server import build_server


SOURCE = 'https://github.com/example-org/example-repo/issues/42'


def git(path, *args):
    return subprocess.run(['git', '-C', str(path), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def flow(tmp_path, monkeypatch):
    monkeypatch.setattr('starforge_workbench.flow._inside_checkout', lambda root: False)
    runtime = tmp_path / 'runtime'
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(runtime))
    profile = tmp_path / 'private' / 'profile.json'
    init_profile(tmp_path / 'metadata', profile=profile)
    git(tmp_path / 'metadata', 'config', 'user.name', 'Example Operator')
    git(tmp_path / 'metadata', 'config', 'user.email', 'operator@example.com')
    open_item(SOURCE, profile=profile)
    return profile, tmp_path


def mcp_packet(profile, arguments):
    async def run():
        async with Client(build_server(flow_profile=profile)) as connected:
            return (await connected.call_tool('flow_packet', arguments)).structured_content
    return asyncio.run(run())


def test_resume_packet_is_bounded_and_matches_cli_mcp(flow, capsys):
    profile, home = flow
    mutate(SOURCE, 'add', profile=profile, title='Keep scope narrow',
           acceptance='Synthetic tests pass', operation_id='add-01')
    direct = build_packet(SOURCE, profile=profile)
    assert direct['source_issue'] == SOURCE
    assert direct['repositories'] == []
    assert direct['next_suggestion']['title'] == 'Keep scope narrow'
    assert direct['authorization'].startswith('This packet records context only')
    assert direct['delivery'] == 'not sent'
    assert direct['agent_session'] == 'not started'
    assert str(home) not in json.dumps(direct)
    assert work_main(['--profile', str(profile), 'packet', SOURCE]) == 0
    cli = json.loads(capsys.readouterr().out)
    mcp = mcp_packet(profile, {'reference': SOURCE})
    for key in ('work_item_id', 'metadata_revision', 'document_hash', 'tasks', 'repositories'):
        assert cli[key] == mcp[key] == direct[key]


def test_handoff_reports_stale_head_and_keeps_unverified_findings(flow):
    profile, home = flow
    repo = home / 'code'
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(repo)], check=True)
    git(repo, 'config', 'user.name', 'Example Operator')
    git(repo, 'config', 'user.email', 'operator@example.com')
    (repo / 'README.md').write_text('base\n')
    git(repo, 'add', 'README.md')
    git(repo, 'commit', '-qm', 'Base')
    worktrees = home / 'worktrees'
    worktrees.mkdir()
    bind(SOURCE, name='api', repository=repo, worktree_root=worktrees,
         base_ref='main', profile=profile)
    prepared = workspace(SOURCE, profile=profile, create=True)['repositories'][0]
    old_head = prepared['head']
    packet = build_packet(SOURCE, profile=profile, kind='handoff', role='reviewer',
                          expected_heads={'api': old_head}, tests=['pytest passed'],
                          findings=['Review error recovery'],
                          pr_links=['https://github.com/example-org/example-repo/pull/7'])
    assert packet['review_head_status'] == 'matches_selected_heads'
    assert packet['tests'][0]['verification'] == 'caller-provided'
    assert packet['pr_links'][0]['verification'] == 'caller-provided; not fetched'
    assert 'worktree' not in packet['repositories'][0]
    checkout = Path(prepared['worktree'])
    (checkout / 'README.md').write_text('changed\n')
    git(checkout, 'commit', '-qam', 'Change')
    stale = build_packet(SOURCE, profile=profile, kind='handoff', role='reviewer',
                         expected_heads={'api': old_head})
    assert stale['review_head_status'] == 'stale'
    assert stale['repositories'][0]['head_match'] == 'stale'
    assert stale['repositories'][0]['head'] != old_head


def test_packet_preserves_blocked_work_and_reports_omissions(flow):
    profile, _ = flow
    blocked = mutate(SOURCE, 'add', profile=profile, title='Await review', operation_id='add-01')
    mutate(SOURCE, 'block', profile=profile, task_id=blocked['task_id'],
           reason='Waiting for source review', operation_id='block-01')
    packet = build_packet(SOURCE, profile=profile, max_chars=2000)
    assert packet['next_suggestion'] is None
    assert packet['tasks'][0]['blocked'] == 'Waiting for source review'
    assert packet['omissions']['paths_withheld'] is True
    with pytest.raises(ValueError, match='selected role'):
        build_packet(SOURCE, profile=profile, kind='handoff')
    with pytest.raises(ValueError, match='plain HTTPS'):
        build_packet(SOURCE, profile=profile, pr_links=['https://user:secret@example.com/pull/1'])


def test_packet_distinguishes_changed_scope_terminal_outcomes_and_dirty_document(flow):
    profile, home = flow
    first = mutate(SOURCE, 'add', profile=profile, title='Initial scope', operation_id='add-01')
    before = build_packet(SOURCE, profile=profile)
    mutate(SOURCE, 'update', profile=profile, task_id=first['task_id'],
           details='Revised acceptance scope', operation_id='update-01')
    second = mutate(SOURCE, 'add', profile=profile, title='No longer needed', operation_id='add-02')
    changed = build_packet(SOURCE, profile=profile)
    assert changed['metadata_revision'] != before['metadata_revision']
    assert changed['tasks'][0]['details'] == 'Revised acceptance scope'
    mutate(SOURCE, 'complete', profile=profile, task_id=first['task_id'],
           evidence='Synthetic verification', operation_id='complete-01')
    mutate(SOURCE, 'cancel', profile=profile, task_id=second['task_id'],
           reason='Scope removed', operation_id='cancel-01')
    terminal = build_packet(SOURCE, profile=profile)
    assert terminal['tasks'] == []
    assert {'complete', 'cancel'} <= {entry['operation'] for entry in terminal['recent_checkpoints']}
    target = home / 'metadata/work/github/github.com/example-org/example-repo/issue-42/TASKS.md'
    target.write_text(target.read_text() + '\nUncommitted local note.\n')
    interrupted = build_packet(SOURCE, profile=profile)
    assert interrupted['reconciliation']['dirty_target'] is True
    assert interrupted['document_hash'] != terminal['document_hash']


def test_large_task_tree_is_truncated_with_an_explicit_count(flow):
    profile, home = flow
    target = home / 'metadata/work/github/github.com/example-org/example-repo/issue-42/TASKS.md'
    tasks = '\n## P1\n\n' + ''.join(
        f'- [ ] Synthetic task {number}\n  - **ID**: task-{number}\n  - **Details**: bounded example\n\n'
        for number in range(40))
    target.write_text(target.read_text() + tasks)
    packet = build_packet(SOURCE, profile=profile, max_chars=3000)
    assert packet['omissions']['tasks'] > 0
    assert len(json.dumps(packet, ensure_ascii=False)) <= 3000
