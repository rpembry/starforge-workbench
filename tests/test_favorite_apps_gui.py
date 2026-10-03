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

    def apply(self, names, token, *, open_unknown=False, retry_unverified=False):
        self.applied.append((names, token, open_unknown, retry_unverified))
        return {'items': [{'name': name, 'result': 'refused' if name != 'Editor' else 'uncertain',
                           'launch_requested': name == 'Editor',
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
    assert restorer.applied == [(['Editor', 'PWA'], 'synthetic-token', True, False)]
    assert calls[0].count('TRUE') == 1
    assert 'PWA: refuse' in next(arg for arg in calls[1] if arg.startswith('--text='))


def test_gui_cancel_does_not_apply(monkeypatch):
    import starforge_workbench.favorite_apps_ui as ui
    monkeypatch.setattr(ui, 'load_config', lambda _: [{'name': 'Editor'}])
    monkeypatch.setattr(ui.shutil, 'which', lambda _: '/usr/bin/zenity')
    monkeypatch.setattr(ui, '_dialog', lambda *args: SimpleNamespace(returncode=1, stdout=''))
    restorer = Restorer()
    assert run_gui(restorer) == 0
    assert not restorer.applied


def test_default_checkmarks_never_open_apps_without_confirmation(monkeypatch):
    import starforge_workbench.favorite_apps_ui as ui
    monkeypatch.setattr(ui, 'load_config', lambda _: [{'name': 'Editor'}, {'name': 'PWA'}])
    monkeypatch.setattr(ui.shutil, 'which', lambda _: '/usr/bin/zenity')
    shown = []
    def dialog(*args):
        shown.append(args)
        return SimpleNamespace(returncode=1, stdout='')
    monkeypatch.setattr(ui, '_dialog', dialog)
    restorer = Restorer()
    assert run_gui(restorer) == 0
    assert shown[0].count('TRUE') == 1
    assert restorer.applied == []


def test_only_verified_absent_apps_start_checked(monkeypatch):
    import starforge_workbench.favorite_apps_ui as ui
    names = ['Absent', 'Present', 'Pending', 'Unknown']
    class Mixed(Restorer):
        def preview(self, selected):
            actions = {'Absent': 'launch', 'Present': 'preserve',
                       'Pending': 'skip', 'Unknown': 'refuse'}
            return {'selection': selected, 'token': 'synthetic-token', 'items': [
                {'name': name, 'action': actions[name],
                 'can_open_anyway': name == 'Unknown', 'evidence': 'synthetic'}
                for name in selected]}
    monkeypatch.setattr(ui, 'load_config', lambda _: [{'name': name} for name in names])
    monkeypatch.setattr(ui.shutil, 'which', lambda _: '/usr/bin/zenity')
    calls = []
    def dialog(*args):
        calls.append(args)
        return SimpleNamespace(returncode=1, stdout='')
    monkeypatch.setattr(ui, '_dialog', dialog)
    restorer = Mixed()
    assert run_gui(restorer) == 0
    rows = calls[0][calls[0].index('--column=Reason') + 1:]
    assert [(rows[i + 1], rows[i]) for i in range(0, len(rows), 4)] == [
        ('Absent', 'TRUE'), ('Present', 'FALSE'),
        ('Pending', 'FALSE'), ('Unknown', 'FALSE')]
    assert restorer.applied == []


def test_preview_back_preserves_selected_rows(monkeypatch):
    import starforge_workbench.favorite_apps_ui as ui
    monkeypatch.setattr(ui, 'load_config', lambda _: [{'name': 'Editor'}])
    monkeypatch.setattr(ui.shutil, 'which', lambda _: '/usr/bin/zenity')
    responses = iter([(0, 'Editor\n'), (1, ''), (0, 'Editor\n'), (0, ''), (0, '')])
    calls = []
    def dialog(*args):
        calls.append(args)
        code, output = next(responses)
        return SimpleNamespace(returncode=code, stdout=output)
    monkeypatch.setattr(ui, '_dialog', dialog)
    restorer = Restorer()
    assert run_gui(restorer) == 0
    assert restorer.applied == [(['Editor'], 'synthetic-token', False, False)]
    assert '--cancel-label=Back' in calls[1]
    assert 'TRUE' in calls[2]


def test_zenity_display_failure_is_not_cancel(monkeypatch):
    import pytest
    import starforge_workbench.favorite_apps_ui as ui
    monkeypatch.setattr(ui, 'load_config', lambda _: [{'name': 'Editor'}])
    monkeypatch.setattr(ui.shutil, 'which', lambda _: '/usr/bin/zenity')
    monkeypatch.setattr(ui.subprocess, 'run', lambda *args, **kwargs:
                        SimpleNamespace(returncode=1, stdout='', stderr='Failed to open display'))
    with pytest.raises(ValueError, match='could not open a graphical dialog'):
        run_gui(Restorer())


def test_unresolved_receipt_requires_separate_confirm_and_back_does_not_retry(monkeypatch):
    import starforge_workbench.favorite_apps_ui as ui
    class Pending(Restorer):
        def preview(self, names):
            return {'selection': names, 'token': 'synthetic-token', 'items': [
                {'name': name, 'action': 'skip', 'can_open_anyway': False,
                 'evidence': 'Prior launch is unresolved; visibility unknown'} for name in names]}
    monkeypatch.setattr(ui, 'load_config', lambda _: [{'name': 'Editor'}])
    monkeypatch.setattr(ui.shutil, 'which', lambda _: '/usr/bin/zenity')
    calls = []
    responses = iter([(0, 'Editor\n'), (1, ''), (1, '')])
    def dialog(*args):
        calls.append(args)
        code, output = next(responses)
        return SimpleNamespace(returncode=code, stdout=output)
    monkeypatch.setattr(ui, '_dialog', dialog)
    restorer = Pending()
    assert run_gui(restorer) == 0
    assert restorer.applied == []
    assert any('retry needs confirmation' in value for value in calls[0])
    assert '--ok-label=I checked; retry' in calls[1]
    assert any('duplicate windows' in value for value in calls[1])


def test_unresolved_receipt_explicit_retry_passes_separate_flag(monkeypatch):
    import starforge_workbench.favorite_apps_ui as ui
    class Pending(Restorer):
        def preview(self, names):
            return {'selection': names, 'token': 'synthetic-token', 'items': [
                {'name': name, 'action': 'skip', 'can_open_anyway': False,
                 'evidence': 'Prior launch is unresolved'} for name in names]}
    monkeypatch.setattr(ui, 'load_config', lambda _: [{'name': 'Editor'}])
    monkeypatch.setattr(ui.shutil, 'which', lambda _: '/usr/bin/zenity')
    responses = iter([(0, 'Editor\n'), (0, ''), (0, '')])
    monkeypatch.setattr(ui, '_dialog', lambda *args: SimpleNamespace(
        returncode=(answer := next(responses))[0], stdout=answer[1]))
    restorer = Pending()
    assert run_gui(restorer) == 0
    assert restorer.applied == [(['Editor'], 'synthetic-token', True, True)]


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


def test_pwa_process_must_match_exact_executable(monkeypatch, tmp_path):
    monkeypatch.setenv('XDG_SESSION_TYPE', 'wayland')
    browser = tmp_path / 'browser'
    other = tmp_path / 'different-browser'
    browser.write_bytes(b'\x7fELFfixture')
    other.write_bytes(b'\x7fELFfixture')
    proc = tmp_path / 'proc'
    (proc / '123').mkdir(parents=True)
    (proc / '123' / 'exe').symlink_to(other)
    (proc / '123' / 'cmdline').write_bytes(
        b'browser\0--profile-directory=Default\0--app-id=synthetic-app\0')
    favorite = {'desktop_id': 'synthetic.desktop', 'kind': 'pwa',
                'profile': 'Default', 'app_id': 'synthetic-app'}
    runner = lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps({'state': 'unknown'}))
    adapter = GnomeDesktop(runner=runner, proc=proc)
    assert adapter.observe(favorite, {'executable': str(browser)})[0] == 'uncertain'
    (proc / '123' / 'exe').unlink()
    (proc / '123' / 'exe').symlink_to(browser)
    assert adapter.observe(favorite, {'executable': str(browser)})[0] == 'present'
