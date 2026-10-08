"""Opt-in owned tmux focus smoke, with shell stand-ins and synthetic identity."""
from contextlib import contextmanager
import os
from pathlib import Path
import pty
import shutil
import subprocess
import time

import pytest

from starforge_workbench.terminal_companion import (ClientView, Companion,
    DisplayedConversation, Pane, TmuxFocusPort)
from tmux_guard import FixtureTmuxTarget, cleanup_fixture_server, require_fixture_socket

pytestmark = pytest.mark.skipif(
    os.environ.get('SF_COMPANION_TMUX_TEST') != '1' or shutil.which('tmux') is None,
    reason='explicit owned tmux fixture opt-in required')


def test_owned_tmux_repeated_summon_return_without_provider_or_shell_input(tmp_path):
    fixture = FixtureTmuxTarget(parent=tmp_path)
    attach = None
    master = slave = None
    def command(*args):
        argv = ['tmux', '-S', str(fixture.socket), *args]
        require_fixture_socket(argv, fixture)
        result = subprocess.run(argv, capture_output=True, text=True, timeout=3)
        assert result.returncode == 0, 'owned tmux fixture operation failed'
        return result.stdout.strip()
    try:
        # Only inert interactive shell stand-ins, no real provider or sent commands.
        shell = command('new-session','-d','-s','synthetic-shell','-P','-F','#{session_id}','/bin/sh')
        target_session = command('new-session','-d','-s','synthetic-codex-standin',
                                 '-P','-F','#{session_id}','/bin/sh')
        def pane(target):
            raw = command('display-message','-p','-t',target,
                '#{socket_path}|#{pid}|#{session_id}|#{window_id}|#{pane_id}|#{pane_pid}|#{pane_current_command}|#{pane_active}|#{window_active}')
            fields = raw.split('|'); assert len(fields) == 9
            return Pane(fields[0], 'server-'+fields[1], fields[2], fields[3], fields[4],
                        int(fields[5]), fields[6], fields[7:]==['1','1']).validate()
        master, slave = pty.openpty()
        argv = ['tmux','-S',str(fixture.socket),'attach-session','-t',shell]
        require_fixture_socket(argv, fixture)
        env = {'PATH':os.environ['PATH'],'TERM':'xterm-256color','HOME':str(tmp_path)}
        attach = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave,
                                  env=env, start_new_session=True)
        clients = ''
        for _ in range(100):
            clients = command('list-clients','-F','#{client_name}|#{client_created}|#{client_pid}')
            if clients: break
            time.sleep(.01)
        rows = clients.splitlines(); assert len(rows) == 1
        client, created, pid = rows[0].split('|')
        # Wait for both fixture execs to settle before recording process metadata.
        for _ in range(100):
            source, target = pane(shell), pane(target_session)
            if source.command == target.command == 'sh': break
            time.sleep(.01)
        assert source.command == target.command == 'sh'
        incarnation = created+':'+pid
        def current():
            session = command('display-message','-p','-c',client,'#{client_session}')
            return ClientView(client, incarnation, pane(session)).validate()
        port = TmuxFocusPort(current=current, observe=pane, tmux=command)
        class SyntheticIdentity:
            in_scope = False
            @contextmanager
            def scope(self, thread, selected):
                assert thread == 'synthetic-thread' and selected == target
                self.in_scope = True
                try: yield
                finally: self.in_scope = False
            def read(self):
                assert self.in_scope
                return DisplayedConversation('synthetic-thread','synthetic-display-generation',pane(target_session))
        state = tmp_path/'focus'; state.mkdir(mode=0o700)
        for _ in range(3):
            owner = Companion(state, port=port, identity=SyntheticIdentity(),
                              target=target, thread_id='synthetic-thread')
            assert owner.toggle()['status'] == 'revealed'
            assert current().pane == target
            assert owner.toggle()['status'] == 'returned'
            assert current().pane == source
            assert pane(source.pane) == source and pane(target.pane) == target
        assert len(command('list-sessions','-F','#{session_id}').splitlines()) == 2
        assert len(command('list-panes','-a','-F','#{pane_id}').splitlines()) == 2
    finally:
        if attach is not None:
            attach.terminate()
            try: attach.wait(timeout=3)
            except subprocess.TimeoutExpired:
                attach.kill(); attach.wait(timeout=3)
        for fd in (master, slave):
            if fd is not None: os.close(fd)
        cleanup_fixture_server(fixture)
        fixture.close()
