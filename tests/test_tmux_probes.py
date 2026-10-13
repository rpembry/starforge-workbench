import copy
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_launcher import cli, ROOT


@pytest.fixture
def context(tmp_path, monkeypatch):
    c = copy.deepcopy(cli.load(ROOT/'config/workbench.example.yaml')['contexts'][0])
    c['cwd'] = str(tmp_path)
    monkeypatch.setattr(cli, 'STATE', tmp_path/'state')
    monkeypatch.setattr(cli, 'TMUX_SOCKET', tmp_path/'isolated.sock')
    # No test in this file can accidentally reach any tmux server.
    monkeypatch.setattr(cli, 'run', Mock(side_effect=AssertionError('unmocked subprocess')))
    return c


def response(stdout='', stderr='', code=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=code)


def test_missing_socket_and_session_are_confirmed_absent(context, monkeypatch):
    mock = Mock(return_value=response(stderr=f'error connecting to {cli.TMUX_SOCKET} (No such file or directory)', code=1))
    monkeypatch.setattr(cli, 'run', mock)
    assert cli.live(context) is None
    assert mock.call_args.kwargs['timeout'] == cli.TMUX_TIMEOUT
    mock.return_value = response('different-context|$2\n')
    assert cli.live(context) is None
    mock.return_value = response(stderr='no sessions', code=1)
    assert cli.live(context) is None


@pytest.mark.parametrize('result,reason', [
    (response(stderr='protocol version mismatch (client 8, server 7)', code=1), 'protocol_mismatch'),
    (response(stderr='cannot connect: Connection refused', code=1), 'probe_failed'),
    (response('unstructured-output'), 'malformed_session_list'),
    (response(''), 'empty_session_list'),
    (response('ok|$1\nok|$2\n'), 'ambiguous_session_list'),
    (response('other|$1\n', stderr='unexpected warning'), 'unexpected_diagnostics'),
])
def test_unknown_metadata_is_never_absence(context, monkeypatch, result, reason):
    monkeypatch.setattr(cli, 'run', Mock(return_value=result))
    with pytest.raises(cli.TmuxUnknown, match=reason): cli.live(context)


def test_timeout_is_bounded_and_diagnostics_do_not_leak(context, monkeypatch):
    run = Mock(side_effect=subprocess.TimeoutExpired('sensitive command', 3, output='PRIVATE OUTPUT', stderr='PRIVATE ERROR'))
    monkeypatch.setattr(cli, 'run', run)
    with pytest.raises(cli.TmuxUnknown) as error: cli.live(context)
    assert 'timeout' in str(error.value) and 'PRIVATE' not in str(error.value)
    assert run.call_args.kwargs['timeout'] == 3


def test_missing_socket_claim_with_existing_socket_is_unknown(context, monkeypatch):
    cli.TMUX_SOCKET.write_text('fixture')
    monkeypatch.setattr(cli, 'run', Mock(return_value=response(stderr=f'error connecting to {cli.TMUX_SOCKET} (No such file or directory)', code=1)))
    with pytest.raises(cli.TmuxUnknown): cli.live(context)


@pytest.mark.parametrize('pane', [response(stderr="can't find session: $1", code=1), response('invalid'), response('0|9|bad|0'), response('0|0||42')])
def test_disappearance_or_malformed_pane_after_present_list_is_unknown(context, monkeypatch, pane):
    monkeypatch.setattr(cli, 'run', Mock(side_effect=[response(cli.session(context)+'|$1\n'), pane]))
    with pytest.raises(cli.TmuxUnknown): cli.live(context)


def test_unknown_up_bind_attach_never_mutates_or_cleans(context, monkeypatch, tmp_path):
    probe = Mock(side_effect=cli.TmuxUnknown('timeout'))
    monkeypatch.setattr(cli, 'probe', probe)
    monkeypatch.setattr(cli, 'validate_context', Mock())
    monkeypatch.setattr(cli.sys.stdin, 'isatty', lambda: True)
    mutation = Mock(side_effect=AssertionError('mutation on unknown state'))
    monkeypatch.setattr(cli, 'tmux', mutation)
    monkeypatch.setattr(cli, 'atomic', mutation)
    monkeypatch.setattr(cli, 'open_tab', mutation)
    cli.STATE.mkdir(mode=0o700)
    pending = cli.STATE/(context['id']+'-pending.json')
    pending.write_text('fixture receipt')
    for operation in [lambda: cli.up(context, ROOT/'config/workbench.example.yaml', headless=True),
                      lambda: cli.bind_session(context, 'fixture'), lambda: cli.attach(context)]:
        with pytest.raises(cli.TmuxUnknown): operation()
    mutation.assert_not_called()
    assert pending.read_text() == 'fixture receipt'


def test_healthy_probe_recovers_and_same_target_is_reused(context, monkeypatch):
    good = [response(cli.session(context)+'|$9\n'), response('0|0|'+cli.fingerprint(context)+'|42\n')]
    run = Mock(side_effect=[subprocess.TimeoutExpired('tmux', 3), *good])
    monkeypatch.setattr(cli, 'run', run)
    with pytest.raises(cli.TmuxUnknown): cli.live(context)
    assert cli.live(context) == dict(attached=0, dead=False, pane_pid=42, identity='$9')


def test_managed_ttys_uses_common_probe_and_rejects_failure(context, monkeypatch):
    run = Mock(return_value=response('0|/dev/pts/42\n0|/dev/pts/43\n1|\n'))
    monkeypatch.setattr(cli, 'run', run)
    assert cli.managed_ttys() == {'/dev/pts/42', '/dev/pts/43'}
    assert run.call_args.kwargs['timeout'] == 3
    run.return_value = response(stderr='protocol version mismatch', code=1)
    with pytest.raises(cli.TmuxUnknown): cli.managed_ttys()
    run.return_value = response('not a tty')
    with pytest.raises(cli.TmuxUnknown): cli.managed_ttys()


def test_status_and_doctor_report_other_contexts_after_uncertainty(context, monkeypatch, capsys):
    other = {**context, 'id':'other'}
    monkeypatch.setattr(cli, 'load', lambda _: {'contexts':[context, other]})
    monkeypatch.setattr(cli, 'validate_context', Mock())
    monkeypatch.setattr(cli, 'saved_session', Mock(return_value=None))
    monkeypatch.setattr(cli, 'external_session', Mock(return_value=None))
    for command in ['status', 'doctor']:
        monkeypatch.setattr(cli, 'live', Mock(side_effect=[cli.TmuxUnknown('timeout'), None]))
        with pytest.raises(ValueError): cli.main([command])
        text = capsys.readouterr().out
        assert 'unknown' in text and 'other' in text and 'absent' in text


def test_interactive_attach_is_unbounded_but_cleanup_requires_known_state(context, monkeypatch):
    state = dict(attached=0, dead=False, pane_pid=42, identity='$7')
    monkeypatch.setattr(cli, 'live', Mock(side_effect=[state, cli.TmuxUnknown('timeout')]))
    monkeypatch.setattr(cli, 'validate_context', Mock())
    monkeypatch.setattr(cli, 'set_title', Mock())
    monkeypatch.setattr(cli.sys.stdin, 'isatty', lambda: True)
    run = Mock(return_value=response())
    monkeypatch.setattr(cli, 'run', run)
    cli.STATE.mkdir(mode=0o700)
    pending = cli.STATE/(context['id']+'-pending.json');pending.write_text('receipt')
    with pytest.raises(cli.TmuxUnknown): cli.attach(context)
    assert 'attach-session' in run.call_args.args[0]
    assert run.call_args.args[0][-1] == '$7'
    assert 'timeout' not in run.call_args.kwargs
    assert pending.exists()


def test_session_lost_after_creation_does_not_set_options_on_another_target(context, monkeypatch):
    monkeypatch.setattr(cli, 'live', Mock(return_value=None))
    monkeypatch.setattr(cli, 'external_session', Mock(return_value=None))
    monkeypatch.setattr(cli, 'validate_context', Mock())
    monkeypatch.setattr(cli, 'saved_session', Mock(return_value=None))
    monkeypatch.setattr(cli, 'target', Mock(return_value=None))
    mutation = Mock(return_value=response())
    monkeypatch.setattr(cli, 'tmux', mutation)
    with pytest.raises(cli.TmuxUnknown, match='disappeared'):
        cli.up(context, ROOT/'config/workbench.example.yaml', headless=True)
    assert mutation.call_count == 1 and mutation.call_args.args[0] == 'new-session'


def test_pane_start_verifies_cwd_and_birth_geometry(context, monkeypatch):
    run = Mock(side_effect=[response(str(cli.cwd(context))+'\n'), response('132x41\n')])
    monkeypatch.setattr(cli, 'run', run)
    monkeypatch.setattr(cli, 'validate_context', Mock())
    cli.verify_pane_start(context, '$7', (132, 41))
    assert all(call.kwargs['cwd'] == cli.TMUX_CLIENT_CWD for call in run.call_args_list)


def test_pane_start_rejects_removed_or_fallback_cwd(context, monkeypatch):
    validate = Mock(side_effect=ValueError('Missing directory'))
    monkeypatch.setattr(cli, 'validate_context', validate)
    with pytest.raises(ValueError, match='Missing directory'):
        cli.verify_pane_start(context, '$7')
    validate.side_effect = None
    monkeypatch.setattr(cli, 'run', Mock(return_value=response('/\n')))
    with pytest.raises(ValueError, match='pane cwd mismatch'):
        cli.verify_pane_start(context, '$7')


def test_tmux_clients_always_start_from_stable_non_project_directory(context, monkeypatch):
    run = Mock(return_value=response())
    monkeypatch.setattr(cli, 'run', run)
    cli.tmux('list-sessions')
    assert run.call_args.kwargs['cwd'] == Path('/')


def test_up_continues_after_unknown_context_without_cleanup(context, monkeypatch):
    other = {**context, 'id':'other'}
    monkeypatch.setattr(cli, 'load', lambda _: {'contexts':[context, other]})
    up = Mock(side_effect=[cli.TmuxUnknown('timeout'), None])
    monkeypatch.setattr(cli, 'up', up)
    with pytest.raises(ValueError, match='Some contexts'):
        cli.main(['up', context['id'], 'other', '--headless'])
    assert up.call_count == 2
