"""Synthetic exact-target specialist handoff checks."""
import copy
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from starforge_workbench import agent_roster, cli, codex_daemon


ROOT = Path(__file__).resolve().parents[1]
SESSION = '11111111-2222-3333-4444-555555555555'
KEY = 'synthetic-key-123456789'


def setup(tmp_path):
    data = yaml.safe_load((ROOT/'config/workbench.example.yaml').read_text())
    context = copy.deepcopy(data['contexts'][0])
    context.update(id='synthetic-support', title='Synthetic Support', cwd=str(tmp_path),
                   enabled=True, codex_remote_daemon=True,
                   resume_policy='explicit-session')
    data['contexts'] = [context]
    manifest = tmp_path/'manifest.yaml'
    manifest.write_text(yaml.safe_dump(data))
    roster = tmp_path/'roster.yaml'
    roster.write_text(yaml.safe_dump({'version': 1, 'agents': [
        {'context_id': 'synthetic-support', 'description': 'Handles synthetic host diagnostics.'}]}))
    roster.chmod(0o600)
    return manifest, roster


def test_list_and_preview_use_exact_private_roster(tmp_path):
    manifest, roster = setup(tmp_path)
    assert agent_roster.list_agents(manifest, roster)[0]['routable']
    with patch.object(cli, 'saved_session', return_value={'id': SESSION}), \
            patch.object(codex_daemon, 'thread_status', return_value='idle'):
        assert agent_roster.preview(manifest, roster, 'synthetic-support') == {
            'outcome': 'ready', 'context_id': 'synthetic-support',
            'session_id': SESSION, 'status': 'idle'}
    roster.chmod(0o644)
    with pytest.raises(ValueError, match='mode 0600'):
        agent_roster.list_agents(manifest, roster)


def test_send_once_records_acceptance_and_reconciles_same_key(tmp_path):
    manifest, roster = setup(tmp_path)
    with patch.object(cli, 'STATE', tmp_path/'state'), \
            patch.object(cli, 'saved_session', return_value={'id': SESSION}), \
            patch.object(codex_daemon, 'thread_status', return_value='idle'), \
            patch.object(codex_daemon, 'start_turn', return_value='turn-synthetic') as start:
        first = agent_roster.send(manifest, roster, 'synthetic-support', SESSION,
                                  'Synthetic request', KEY)
        second = agent_roster.send(manifest, roster, 'synthetic-support', SESSION,
                                   'Synthetic request', KEY)
        assert first == second
        assert first['outcome'] == 'accepted'
        assert first['turn_id'] == 'turn-synthetic'
        start.assert_called_once_with(SESSION, 'Synthetic request')
        with pytest.raises(ValueError, match='different request'):
            agent_roster.send(manifest, roster, 'synthetic-support', SESSION,
                              'Different request', KEY)


def test_uncertain_send_is_never_replayed(tmp_path):
    manifest, roster = setup(tmp_path)
    with patch.object(cli, 'STATE', tmp_path/'state'), \
            patch.object(cli, 'saved_session', return_value={'id': SESSION}), \
            patch.object(codex_daemon, 'thread_status', return_value='idle'), \
            patch.object(codex_daemon, 'start_turn', side_effect=TimeoutError('synthetic')) as start:
        result = agent_roster.send(manifest, roster, 'synthetic-support', SESSION,
                                   'Synthetic request', KEY)
        assert result['outcome'] == 'uncertain'
        assert agent_roster.send(manifest, roster, 'synthetic-support', SESSION,
                                 'Synthetic request', KEY) == result
        start.assert_called_once()


def test_crash_after_request_journal_is_not_replayed(tmp_path):
    manifest, roster = setup(tmp_path)
    with patch.object(cli, 'STATE', tmp_path/'state'), \
            patch.object(cli, 'saved_session', return_value={'id': SESSION}), \
            patch.object(codex_daemon, 'thread_status', return_value='idle'), \
            patch.object(codex_daemon, 'start_turn', side_effect=SystemExit('synthetic crash')) as start:
        with pytest.raises(SystemExit):
            agent_roster.send(manifest, roster, 'synthetic-support', SESSION,
                              'Synthetic request', KEY)
        assert agent_roster.send(manifest, roster, 'synthetic-support', SESSION,
                                 'Synthetic request', KEY)['outcome'] == 'uncertain'
        start.assert_called_once()


def test_busy_or_changed_target_is_not_sent(tmp_path):
    manifest, roster = setup(tmp_path)
    with patch.object(cli, 'STATE', tmp_path/'state'), \
            patch.object(cli, 'saved_session', return_value={'id': SESSION}), \
            patch.object(codex_daemon, 'thread_status', return_value='active'), \
            patch.object(codex_daemon, 'start_turn') as start:
        assert agent_roster.send(manifest, roster, 'synthetic-support', SESSION,
                                 'Synthetic request', KEY)['outcome'] == 'unavailable'
        start.assert_not_called()
    with patch.object(cli, 'STATE', tmp_path/'state'), \
            patch.object(cli, 'saved_session', return_value={'id': '22222222-2222-2222-2222-222222222222'}), \
            patch.object(codex_daemon, 'start_turn') as start:
        assert agent_roster.send(manifest, roster, 'synthetic-support', SESSION,
                                 'Synthetic request', KEY)['reason'] == 'binding_changed'
        start.assert_not_called()


def test_handoff_rejects_context_path_before_lock(tmp_path):
    manifest, roster = setup(tmp_path)
    with patch.object(cli, 'STATE', tmp_path/'state'), \
            patch.object(cli, 'lock', side_effect=AssertionError('lock reached')):
        with pytest.raises(ValueError, match='valid roster context'):
            agent_roster.send(manifest, roster, '../other', SESSION, 'Synthetic', KEY)
