"""Test-only guard that prevents tmux fixtures from reaching live servers."""
import os
from pathlib import Path
import subprocess


def require_fixture_socket(args, expected_socket, environ=None):
    environ = os.environ if environ is None else environ
    if Path(args[0]).name != 'tmux':
        return
    if '-L' in args or args.count('-S') != 1:
        raise AssertionError('tmux tests require one explicit fixture socket; named/default servers are forbidden')
    index = args.index('-S')
    if index+1 >= len(args):
        raise AssertionError('tmux fixture socket value is missing')
    expected = Path(expected_socket).resolve()
    actual = Path(args[index+1]).resolve()
    default = Path(environ.get('TMUX_TMPDIR', '/tmp')).resolve()/('tmux-'+str(os.getuid()))/'default'
    inherited = environ.get('TMUX', '').split(',', 1)[0]
    if actual == default or (inherited and actual == Path(inherited).resolve()):
        raise AssertionError('tmux tests cannot target the default or inherited live socket')
    if actual != expected:
        raise AssertionError('tmux command does not target this fixture socket')


def guarded_tmux_run(original_run, expected_socket, environ=None):
    def isolated(args, **kwargs):
        require_fixture_socket(args, expected_socket, environ)
        return original_run(args, **kwargs)
    return isolated


def cleanup_fixture_server(socket, run=subprocess.run):
    args = ['tmux', '-S', str(Path(socket).resolve()), 'kill-server']
    require_fixture_socket(args, socket)
    return run(args, capture_output=True, timeout=3)
