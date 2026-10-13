"""Read-only session selection for the terminal-friendly aiw c command."""
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest

from test_launcher import cli


@pytest.fixture
def contexts():
    return [
        {'id': 'example-support', 'title': 'Example Support'},
        {'id': 'example-dev', 'title': 'Example Dev'},
    ]


def test_exact_multiword_name_id_and_missing_name(contexts):
    assert cli.resolve_connect_context(contexts, ['Example', 'Support']) == contexts[0]
    assert cli.resolve_connect_context(contexts, ['example-support']) == contexts[0]
    with pytest.raises(ValueError, match='Unknown context name'):
        cli.resolve_connect_context(contexts, ['Example'])
    with pytest.raises(ValueError, match='aiw list'):
        cli.resolve_connect_context(contexts, [])


def test_cli_joins_unquoted_words_from_manifest(contexts, monkeypatch):
    selected = Mock()
    monkeypatch.setattr(cli, 'load', lambda _: {'contexts': contexts})
    monkeypatch.setattr(cli, 'connect', selected)
    cli.main(['c', 'Example', 'Support'])
    selected.assert_called_once_with(contexts[0])


def test_ambiguous_name_refuses_to_choose(contexts):
    contexts[1]['title'] = 'Example Support'
    with pytest.raises(ValueError, match='example-support, example-dev'):
        cli.resolve_connect_context(contexts, ['Example', 'Support'])


@pytest.fixture
def terminal(monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(cli.sys.stdout, 'isatty', lambda: True)
    monkeypatch.setattr(cli, 'run', Mock())
    monkeypatch.setattr(cli, 'tmux', Mock())
    monkeypatch.setattr(cli, 'probe', Mock())
    monkeypatch.setattr(cli, 'live', Mock(return_value={'identity': '$7', 'dead': False, 'attached': 1}))
    return {'id': 'example-support', 'title': 'Example Support'}


def test_connect_attaches_second_client_without_detaching_or_creating(terminal, monkeypatch):
    monkeypatch.delenv('TMUX', raising=False)
    cli.connect(terminal)
    cli.run.assert_called_once_with(cli.tmux_command('attach-session', '-t', '$7'),
                                    cwd=Path('/'), check=True)
    cli.tmux.assert_not_called()


def test_connect_switches_only_within_same_server(terminal, monkeypatch):
    monkeypatch.setenv('TMUX', '/tmp/fixture/socket,123,0')
    cli.probe.return_value = '/tmp/fixture/socket\n'
    cli.connect(terminal)
    cli.tmux.assert_called_once_with('switch-client', '-t', '$7')
    cli.run.assert_not_called()

    cli.tmux.reset_mock()
    cli.probe.return_value = '/tmp/other/socket\n'
    with pytest.raises(ValueError, match='different tmux server'):
        cli.connect(terminal)
    cli.tmux.assert_not_called()


@pytest.mark.parametrize('state', [None, {'identity': '$7', 'dead': True, 'attached': 0}])
def test_connect_missing_or_dead_never_mutates(terminal, monkeypatch, state):
    monkeypatch.delenv('TMUX', raising=False)
    cli.live.return_value = state
    with pytest.raises(ValueError, match='no live managed tmux session'):
        cli.connect(terminal)
    cli.run.assert_not_called()
    cli.tmux.assert_not_called()


def test_connect_requires_terminal_before_probe(terminal, monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, 'isatty', lambda: False)
    with pytest.raises(ValueError, match='interactive terminal'):
        cli.connect(terminal)
    cli.live.assert_not_called()


def test_attach_failure_does_not_expose_tmux_output(terminal, monkeypatch):
    monkeypatch.delenv('TMUX', raising=False)
    cli.run.side_effect = subprocess.CalledProcessError(1, ['tmux'], stderr='private output')
    with pytest.raises(cli.TmuxUnknown, match='attach_failed') as error:
        cli.connect(terminal)
    assert 'private output' not in str(error.value)
