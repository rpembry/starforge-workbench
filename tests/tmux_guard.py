"""Test-owned tmux targets that cannot resolve to a live server."""
import os
from pathlib import Path
import secrets
import stat
import subprocess
import tempfile
import time


class FixtureTmuxTarget:
    def __init__(self, parent=None):
        self._temporary = tempfile.TemporaryDirectory(prefix='sfwb-tmux-', dir=parent)
        self._root = Path(self._temporary.name).resolve()
        self._token = secrets.token_hex(32)
        self._marker = self._root/'.fixture-owner'
        fd = os.open(self._marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(self._token)

    @property
    def socket(self):
        return self._root/'fixture.sock'

    def verify_owned(self):
        root = self._root.lstat()
        marker = self._marker.lstat()
        if (stat.S_ISLNK(root.st_mode) or not stat.S_ISDIR(root.st_mode)
                or root.st_uid != os.getuid() or root.st_mode & 0o077):
            raise AssertionError('tmux fixture directory ownership is unsafe')
        if (stat.S_ISLNK(marker.st_mode) or not stat.S_ISREG(marker.st_mode)
                or marker.st_uid != os.getuid() or marker.st_mode & 0o177):
            raise AssertionError('tmux fixture ownership marker is unsafe')
        if self._marker.read_text() != self._token:
            raise AssertionError('tmux fixture ownership marker changed')
        try:
            socket = self.socket.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(socket.st_mode) or socket.st_uid != os.getuid():
            raise AssertionError('tmux fixture socket is unsafe')

    def close(self):
        self.verify_owned()
        if self.socket.exists() or self.socket.is_symlink():
            raise AssertionError('tmux fixture socket still exists; guarded cleanup is required')
        self._temporary.cleanup()


def require_fixture_socket(args, target, environ=None):
    if not isinstance(target, FixtureTmuxTarget):
        raise TypeError('tmux tests require a test-created FixtureTmuxTarget')
    target.verify_owned()
    environ = os.environ if environ is None else environ
    if not args or Path(args[0]).name != 'tmux':
        return

    socket = None
    index = 1
    while index < len(args) and args[index].startswith('-'):
        option = args[index]
        if option in {'-f', '-S'}:
            if index+1 >= len(args):
                raise AssertionError('tmux global option value is missing')
            if option == '-S':
                if socket is not None:
                    raise AssertionError('duplicate tmux socket selector')
                socket = Path(args[index+1]).resolve()
            index += 2
            continue
        raise AssertionError('unsafe tmux global option: '+option)
    if index >= len(args):
        raise AssertionError('tmux command is missing')
    if socket != target.socket:
        raise AssertionError('tmux command does not target the owned fixture socket')

    inherited = environ.get('TMUX', '').split(',', 1)[0]
    if inherited and socket == Path(inherited).resolve():
        raise AssertionError('tmux tests cannot target an inherited live socket')


def guarded_tmux_run(original_run, target, environ=None):
    if not isinstance(target, FixtureTmuxTarget):
        raise TypeError('tmux tests require a test-created FixtureTmuxTarget')

    def isolated(args, **kwargs):
        require_fixture_socket(args, target, environ)
        return original_run(args, **kwargs)
    return isolated


def cleanup_fixture_server(target, run=subprocess.run):
    if not isinstance(target, FixtureTmuxTarget):
        raise TypeError('tmux cleanup requires a test-created FixtureTmuxTarget')
    args = ['tmux', '-S', str(target.socket), 'kill-server']
    require_fixture_socket(args, target)
    try:
        original = target.socket.lstat()
    except FileNotFoundError:
        original = None
    result = run(args, capture_output=True, timeout=3)
    for _ in range(50):
        if not target.socket.exists() and not target.socket.is_symlink():
            break
        time.sleep(.01)
    if original is not None and result.returncode == 0:
        try:
            current = target.socket.lstat()
        except FileNotFoundError:
            current = None
        if current is not None and (current.st_dev, current.st_ino) == (original.st_dev, original.st_ino):
            target.socket.unlink()
    return result
