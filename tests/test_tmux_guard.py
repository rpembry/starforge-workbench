from pathlib import Path
from unittest.mock import Mock

import pytest

from tmux_guard import cleanup_fixture_server, guarded_tmux_run


def test_guard_accepts_only_exact_fixture_socket(tmp_path):
    socket = tmp_path/'fixture.sock'
    mutation = Mock(return_value='ok')
    run = guarded_tmux_run(mutation, socket, environ={})
    assert run(['/usr/bin/tmux', '-S', str(socket), 'new-session']) == 'ok'
    mutation.assert_called_once()


@pytest.mark.parametrize('args', [
    ['/usr/bin/tmux', 'new-session'],
    ['/usr/bin/tmux', '-L', 'starforge-ai-workbench', 'new-session'],
    ['/usr/bin/tmux', '-S', '/tmp/other.sock', 'new-session'],
])
def test_guard_rejects_missing_named_or_wrong_target_before_mutation(tmp_path, args):
    mutation = Mock()
    run = guarded_tmux_run(mutation, tmp_path/'fixture.sock', environ={})
    with pytest.raises(AssertionError):
        run(args)
    mutation.assert_not_called()


def test_guard_rejects_default_and_inherited_targets_even_without_tmux_env(tmp_path):
    mutation = Mock()
    default = Path('/tmp')/('tmux-'+str(__import__('os').getuid()))/'default'
    for socket, environ in [(default, {}), (tmp_path/'live.sock', {'TMUX': str(tmp_path/'live.sock')+',1,0'})]:
        run = guarded_tmux_run(mutation, socket, environ=environ)
        with pytest.raises(AssertionError):
            run(['/usr/bin/tmux', '-S', str(socket), 'kill-server'])
    mutation.assert_not_called()


def test_cleanup_is_limited_to_exact_fixture_socket(tmp_path):
    run = Mock()
    socket = tmp_path/'fixture.sock'
    cleanup_fixture_server(socket, run=run)
    assert run.call_args.args[0] == ['tmux', '-S', str(socket.resolve()), 'kill-server']
