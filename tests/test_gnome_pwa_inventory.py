from types import SimpleNamespace
from pathlib import Path

from starforge_workbench.gnome_presence import GnomeDesktop


def test_unreadable_process_does_not_hide_later_exact_pwa(monkeypatch, tmp_path):
    monkeypatch.setenv('XDG_SESSION_TYPE', 'wayland')
    proc = tmp_path / 'proc'
    proc.mkdir()
    executable = tmp_path / 'browser'
    executable.write_bytes(b'\x7fELF')
    for pid in ['100', '200']:
        process = proc / pid
        process.mkdir()
        (process / 'exe').symlink_to(executable)
        (process / 'cmdline').write_bytes(b'browser\0--profile-directory=Profile 2\0--app-id=example-app\0')
    resolve = Path.resolve
    def protected(path, *args, **kwargs):
        if path == proc / '100' / 'exe':
            raise PermissionError('synthetic protected process')
        return resolve(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'resolve', protected)
    desktop = GnomeDesktop(proc=proc, runner=lambda *args, **kwargs: SimpleNamespace(stdout='{"state":"unknown"}'))
    favorite = {'kind':'pwa','desktop_id':'example.desktop','profile':'Profile 2','app_id':'example-app'}
    assert desktop.observe(favorite, {'executable':str(executable)})[0] == 'present'
    (proc / '200' / 'cmdline').write_bytes(b'browser\0--profile-directory=Profile 3\0--app-id=example-app\0')
    assert desktop.observe(favorite, {'executable':str(executable)})[0] == 'uncertain'
