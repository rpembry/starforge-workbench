"""The graphical flow must never turn an unknown status into a launch."""
import json
from pathlib import Path
from types import SimpleNamespace

from starforge_workbench.favorite_apps_ui import run_gui
from starforge_workbench.gnome_presence import GnomeDesktop


class Restorer:
    config = Path('/synthetic/favorites.yaml')

    def __init__(self):
        self.applied = []

    def preview(self, names):
        return {'selection': names, 'token': 'synthetic-token', 'items': [
            {'name': name, 'action': 'launch' if name == 'Editor' else 'refuse',
             'can_open_anyway': name == 'PWA',
             'evidence': 'synthetic evidence'} for name in names]}

    def apply(self, names, token, *, open_unknown=False):
        self.applied.append((names, token, open_unknown))
        return {'items': [{'name': name, 'result': 'refused' if name != 'Editor' else 'uncertain',
                           'evidence': 'synthetic result'} for name in names]}


def test_gui_requires_review_then_uses_exact_preview_token(monkeypatch):
    import starforge_workbench.favorite_apps_ui as ui
    monkeypatch.setattr(ui, 'load_config', lambda _: [{'name': 'Editor'}, {'name': 'PWA'}])
    monkeypatch.setattr(ui.shutil, 'which', lambda _: '/usr/bin/zenity')
    calls = []
    outputs = ['Editor\nPWA\n', '', '', '']
    def dialog(*args):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=outputs.pop(0))
    monkeypatch.setattr(ui, '_dialog', dialog)
    restorer = Restorer()
    assert run_gui(restorer) == 0
    assert restorer.applied == [(['Editor', 'PWA'], 'synthetic-token', True)]
    assert 'PWA: refuse' in next(arg for arg in calls[1] if arg.startswith('--text='))


def test_gui_cancel_does_not_apply(monkeypatch):
    import starforge_workbench.favorite_apps_ui as ui
    monkeypatch.setattr(ui, 'load_config', lambda _: [{'name': 'Editor'}])
    monkeypatch.setattr(ui.shutil, 'which', lambda _: '/usr/bin/zenity')
    monkeypatch.setattr(ui, '_dialog', lambda *args: SimpleNamespace(returncode=1, stdout=''))
    restorer = Restorer()
    assert run_gui(restorer) == 0
    assert not restorer.applied


def test_gnome_adapter_rejects_unknown_and_unverified_process(monkeypatch, tmp_path):
    monkeypatch.setenv('XDG_SESSION_TYPE', 'wayland')
    proc = tmp_path / 'proc'
    proc.mkdir()
    executable = tmp_path / 'editor'
    executable.write_bytes(b'\x7fELFfixture')
    entry = {'executable': str(executable)}
    favorite = {'desktop_id': 'editor.desktop'}
    result = ['unknown']
    def runner(*args, **kwargs):
        return SimpleNamespace(stdout=json.dumps({'state': result[0]}))
    adapter = GnomeDesktop(runner=runner, proc=proc)
    assert adapter.observe(favorite, entry)[0] == 'uncertain'
    result[0] = 'absent'
    assert adapter.observe(favorite, entry)[0] == 'absent'
    (proc / '123').mkdir()
    (proc / '123' / 'exe').symlink_to(executable)
    assert adapter.observe(favorite, entry)[0] == 'uncertain'
    (proc / '123' / 'exe').unlink()
    executable.write_text('#!/bin/sh\n')
    assert adapter.observe(favorite, entry)[0] == 'uncertain'
