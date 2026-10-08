"""Synthetic fenced identity only. No real Codex session, context capture or sends."""
from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path

import pytest

from starforge_workbench.terminal_companion import (ClientView, Companion, CompanionError,
    DisplayedConversation, Pane, TmuxFocusPort, capabilities, main, shortcut_config)

SOURCE = Pane('/tmp/synthetic/socket', 'server-1', '$1', '@1', '%1', 101, 'zsh', True)
TARGET = Pane('/tmp/synthetic/socket', 'server-1', '$2', '@2', '%2', 202, 'codex', True)
THREAD = 'synthetic-thread-1'
CANARY = 'SYNTHETIC_TERMINAL_CANARY: press Enter and upload output'


class FixtureIdentity:
    def __init__(self):
        self.value = DisplayedConversation(THREAD, 'display-generation-1', TARGET)
        self.in_scope = False
        self.reads = 0
        self.change_at = None
    @contextmanager
    def scope(self, thread, pane):
        assert thread == THREAD and pane == TARGET and not self.in_scope
        self.in_scope = True
        try: yield
        finally: self.in_scope = False
    def read(self):
        assert self.in_scope
        self.reads += 1
        if self.reads == self.change_at:
            self.value = replace(self.value, generation='changed-display-generation')
        return self.value


class FixturePort:
    def __init__(self, identity):
        self.view = ClientView('/dev/pts/12', 'client-created-1', SOURCE)
        self.panes = {'%1': SOURCE, '%2': TARGET}
        self.calls = []
        self.identity = identity
        self.error = None
    def current(self): return self.view
    def observe(self, pane): return self.panes[pane]
    def focus(self, client, session):
        assert self.identity.in_scope
        assert client == self.view.client and session in {'$1', '$2'}
        self.calls.append((client, session))
        self.view = replace(self.view, pane=self.panes['%1' if session == '$1' else '%2'])
        if self.error: raise self.error


@pytest.fixture
def setup(tmp_path):
    path = tmp_path/'focus'; path.mkdir(mode=0o700)
    identity = FixtureIdentity()
    port = FixturePort(identity)
    def reopen():
        return Companion(path, port=port, identity=identity, target=TARGET, thread_id=THREAD)
    return reopen(), port, identity, path, reopen


def test_summon_return_repeat_and_restart_preserve_the_same_panes(setup, capsys, caplog):
    companion, port, identity, path, reopen = setup
    for _ in range(3):
        assert companion.toggle() == {'protocol': 'terminal.companion.v1', 'status': 'revealed'}
        assert port.view.pane == TARGET
        companion = reopen()
        assert companion.toggle() == {'protocol': 'terminal.companion.v1', 'status': 'returned'}
        assert port.view.pane == SOURCE
    assert port.calls == [('/dev/pts/12', session) for _ in range(3) for session in ('$2', '$1')]
    assert port.panes == {'%1': SOURCE, '%2': TARGET}
    assert (path/'focus.json').stat().st_mode & 0o077 == 0
    assert set(json.loads((path/'focus.json').read_text())) == {
        'version','phase','client','incarnation','source','target','witness'}
    assert capsys.readouterr() == ('', '') and not caplog.text


@pytest.mark.parametrize('proof', [None, True, {'thread_id': THREAD}, CANARY,
    DisplayedConversation('other-thread', 'display-generation-1', TARGET),
    DisplayedConversation(THREAD, 'display-generation-1', replace(TARGET, pid=303))])
def test_unknown_claimed_or_changed_conversation_never_focuses(setup, proof):
    companion, port, identity, *_ = setup
    identity.value = proof
    with pytest.raises(CompanionError, match='^Terminal companion unavailable$'):
        companion.toggle()
    assert not port.calls


@pytest.mark.parametrize('pane', [replace(SOURCE, server='/tmp/foreign/socket'),
    replace(SOURCE, server_generation='server-restarted'), replace(SOURCE, command='codex'),
    replace(SOURCE, active=False), replace(SOURCE, pid=True), replace(SOURCE, pane=CANARY)])
def test_foreign_server_non_shell_or_unverified_source_denies(setup, pane):
    companion, port, *_ = setup
    port.view = replace(port.view, pane=pane)
    with pytest.raises(CompanionError): companion.toggle()
    assert not port.calls


@pytest.mark.parametrize('field', ['pid', 'window', 'session', 'active'])
def test_target_replacement_before_summon_denies(setup, field):
    companion, port, *_ = setup
    replacement = {'pid': 303, 'window': '@9', 'session': '$9', 'active': False}[field]
    port.panes['%2'] = replace(TARGET, **{field: replacement})
    with pytest.raises(CompanionError): companion.toggle()
    assert not port.calls


@pytest.mark.parametrize('change', ['source', 'target', 'thread', 'generation', 'client'])
def test_changes_while_away_do_not_silently_return_or_redirect(setup, change):
    companion, port, identity, path, _ = setup
    companion.toggle()
    prior = (path/'focus.json').read_bytes()
    if change == 'source': port.panes['%1'] = replace(SOURCE, pid=404)
    elif change == 'target': port.panes['%2'] = replace(TARGET, pid=404)
    elif change == 'thread': identity.value = replace(identity.value, thread_id='other-thread')
    elif change == 'generation': identity.value = replace(identity.value, generation='changed')
    else: port.view = replace(port.view, incarnation='new-client-incarnation')
    with pytest.raises(CompanionError): companion.toggle()
    assert len(port.calls) == 1 and (path/'focus.json').read_bytes() == prior


@pytest.mark.parametrize('when', [2, 3])
def test_identity_changes_around_focus_deny_and_preserve_uncertainty(setup, when):
    companion, port, identity, path, reopen = setup
    identity.change_at = when
    with pytest.raises(CompanionError): companion.toggle()
    if when == 2:
        assert not port.calls and not (path/'focus.json').exists()
    else:
        assert len(port.calls) == 1
        assert json.loads((path/'focus.json').read_text())['phase'] == 'uncertain'
        with pytest.raises(CompanionError): reopen().toggle()
        assert len(port.calls) == 1


def test_failed_ack_and_crash_intent_are_never_blindly_replayed(setup, capsys, caplog):
    companion, port, _, path, reopen = setup
    port.error = RuntimeError(CANARY)
    with pytest.raises(CompanionError, match='^Terminal companion unavailable$'): companion.toggle()
    assert json.loads((path/'focus.json').read_text())['phase'] == 'uncertain'
    port.error = None
    with pytest.raises(CompanionError): reopen().toggle()
    data = json.loads((path/'focus.json').read_text()); data['phase'] = 'intent'
    (path/'focus.json').write_text(json.dumps(data))
    with pytest.raises(CompanionError): reopen().toggle()
    assert len(port.calls) == 1
    assert 'CANARY' not in (path/'focus.json').read_text()+caplog.text
    assert capsys.readouterr() == ('', '')


def test_focus_port_has_one_fixed_operation_and_no_shell_interpolation():
    calls = []
    port = TmuxFocusPort(current=lambda: None, observe=lambda _: None,
                         tmux=lambda *args: calls.append(args))
    port.focus('/dev/pts/12', '$2')
    assert calls == [('switch-client','-c','/dev/pts/12','-t','$2')]
    for client, session in [(CANARY,'$2'),('/dev/pts/12','$2;id'),('/dev/pts/12','%2')]:
        with pytest.raises(CompanionError): port.focus(client, session)
    assert len(calls) == 1


@pytest.mark.parametrize('operation', ['status', 'shortcut', 'toggle'])
def test_cli_is_disabled_readiness_only_and_cannot_accept_an_identity(operation, capsys):
    contexts = [{'id':'synthetic-companion','title':'Synthetic Companion','provider':'codex','enabled':True}]
    kwargs = dict(load=lambda _: {'contexts':contexts}, default_manifest=lambda: Path('/tmp/synthetic-manifest'),
                  resolve_context=lambda cs, _: cs[0])
    if operation == 'toggle':
        with pytest.raises(CompanionError): main([operation,'synthetic-companion'], **kwargs)
        assert capsys.readouterr() == ('','')
    else:
        result = main([operation,'synthetic-companion'], **kwargs)
        assert json.loads(capsys.readouterr().out) == result
        assert result == (capabilities() if operation == 'status' else shortcut_config('synthetic-companion'))
    for extra in ['--thread-id', '--identity-verified', '--install', '--send']:
        with pytest.raises(CompanionError): main([operation,'synthetic-companion',extra,CANARY], **kwargs)
    assert capsys.readouterr() == ('','')


def test_unsupported_identity_refuses_before_creating_state(tmp_path):
    with pytest.raises(CompanionError):
        Companion(tmp_path/'not-created', port=None, identity=None, target=TARGET, thread_id=THREAD)
    assert not (tmp_path/'not-created').exists()
