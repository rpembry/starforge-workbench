import os
from pathlib import Path
import socket
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tmux_guard import FixtureTmuxTarget, cleanup_fixture_server, guarded_tmux_run


def target(tmp_path):
    return FixtureTmuxTarget(tmp_path)


def test_guard_accepts_exact_owned_socket_and_ignores_command_options(tmp_path):
    owned = target(tmp_path)
    mutation = Mock(return_value='ok')
    run = guarded_tmux_run(mutation, owned, environ={})
    args = ['/usr/bin/tmux', '-f', '/fixture/tmux.conf', '-S', str(owned.socket),
            'new-session', 'provider', '-S/not-a-global-selector']
    assert run(args) == 'ok'
    mutation.assert_called_once()
    owned.close()


@pytest.mark.parametrize('suffix', [
    [],
    ['-L', 'starforge-ai-workbench', 'new-session'],
    ['-Lstarforge-ai-workbench', 'new-session'],
    ['-S/alternate.sock', 'new-session'],
    ['-S', '/tmp/first.sock', '-S', '/tmp/second.sock', 'new-session'],
    ['-S', '/tmp/first.sock', '-S/tmp/second.sock', 'kill-server'],
])
def test_guard_rejects_missing_named_attached_and_duplicate_selectors_before_subprocess(tmp_path, suffix):
    owned = target(tmp_path)
    mutation = Mock()
    run = guarded_tmux_run(mutation, owned, environ={})
    with pytest.raises(AssertionError):
        run(['/usr/bin/tmux', *suffix])
    mutation.assert_not_called()
    owned.close()


def test_arbitrary_default_and_production_paths_cannot_be_blessed_or_cleaned(tmp_path):
    mutation = Mock()
    production = Path('/tmp')/('tmux-'+str(os.getuid()))/'starforge-ai-workbench'
    default = production.with_name('default')
    for unsafe in [production, default, tmp_path/'arbitrary-live.sock']:
        with pytest.raises(TypeError):
            guarded_tmux_run(mutation, unsafe, environ={})
        with pytest.raises(TypeError):
            cleanup_fixture_server(unsafe, run=mutation)
    mutation.assert_not_called()


def test_inherited_or_symlinked_owned_target_is_rejected_before_subprocess(tmp_path):
    owned = target(tmp_path)
    mutation = Mock()
    run = guarded_tmux_run(mutation, owned, environ={'TMUX': str(owned.socket)+',1,0'})
    with pytest.raises(AssertionError, match='inherited'):
        run(['/usr/bin/tmux', '-S', str(owned.socket), 'new-session'])
    mutation.assert_not_called()

    owned.socket.symlink_to(tmp_path/'possible-live.sock')
    with pytest.raises(AssertionError, match='socket is unsafe'):
        guarded_tmux_run(mutation, owned, environ={})(
            ['/usr/bin/tmux', '-S', str(owned.socket), 'new-session'])
    with pytest.raises(AssertionError, match='socket is unsafe'):
        cleanup_fixture_server(owned, run=mutation)
    mutation.assert_not_called()
    owned.socket.unlink()
    owned.close()


def test_changed_ownership_marker_blocks_execution_and_cleanup(tmp_path):
    owned = target(tmp_path)
    mutation = Mock()
    owned._marker.write_text('not-the-owner-token')
    run = guarded_tmux_run(mutation, owned, environ={})
    with pytest.raises(AssertionError, match='marker changed'):
        run(['/usr/bin/tmux', '-S', str(owned.socket), 'new-session'])
    with pytest.raises(AssertionError, match='marker changed'):
        cleanup_fixture_server(owned, run=mutation)
    mutation.assert_not_called()
    owned._marker.write_text(owned._token)
    owned.close()


def test_cleanup_uses_only_the_owned_fixture_socket(tmp_path):
    owned = target(tmp_path)
    run = Mock()
    cleanup_fixture_server(owned, run=run)
    assert run.call_args.args[0] == ['tmux', '-S', str(owned.socket), 'kill-server']
    owned.close()


def test_successful_cleanup_removes_only_same_owned_stale_socket_inode(tmp_path):
    owned = target(tmp_path)
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(owned.socket))
    inode = owned.socket.stat().st_ino
    run = Mock(return_value=SimpleNamespace(returncode=0))
    cleanup_fixture_server(owned, run=run)
    assert not owned.socket.exists()
    assert inode
    server.close()
    owned.close()
