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
        self.attempts = []

    def record_attempt(self, **event):
        self.attempts.append(event)

    def preview(self, names):
        return {'selection': names, 'token': 'synthetic-token', 'items': [
            {'name': name, 'action': 'launch' if name == 'Editor' else 'refuse',
             'can_open_anyway': name == 'PWA',
             'evidence': 'synthetic evidence'} for name in names]}

    def apply(self, names, token, *, open_unknown=False, retry_unverified=False):
        self.applied.append((names, token, open_unknown, retry_unverified))
        return {'items': [{'name': name, 'result': 'refused' if name != 'Editor' else 'uncertain',
                           'launch_requested': name == 'Editor',
                           'launcher_accepted': True if name == 'Editor' else None,
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
    assert restorer.attempts[-1]['phase'] == 'completed'
    assert restorer.attempts[-1]['result_counts']['launch_requested'] == 1
    assert calls[0].count('TRUE') == 2
    assert 'PWA: refuse' in next(arg for arg in calls[1] if arg.startswith('--text='))


def test_gui_cancel_does_not_apply(monkeypatch):
    import starforge_workbench.favorite_apps_ui as ui
    monkeypatch.setattr(ui, 'load_config', lambda _: [{'name': 'Editor'}])
    monkeypatch.setattr(ui.shutil, 'which', lambda _: '/usr/bin/zenity')
    monkeypatch.setattr(ui, '_dialog', lambda *args: SimpleNamespace(returncode=1, stdout=''))
    restorer = Restorer()
    assert run_gui(restorer) == 0
    assert not restorer.applied
    assert restorer.attempts[-1]['phase'] == 'cancelled'


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
    assert shown[0].count('TRUE') == 2
    assert restorer.applied == []
    assert restorer.attempts[-1]['phase'] == 'cancelled'


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
    restorer = Restorer()
    with pytest.raises(ValueError, match='could not open a graphical dialog'):
        run_gui(restorer)
    assert restorer.attempts[-1]['phase'] == 'error'
    assert restorer.attempts[-1]['failure_reason'] == 'display_error'


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


def test_zero_request_headlines_are_truthful():
    from starforge_workbench.favorite_apps_ui import _headline
    counts = {'launch_requested': 0, 'launcher_accepted': 0, 'verified_ready': 0,
              'already_present': 2, 'unresolved': 0, 'refused': 0}
    assert 'already open' in _headline(counts, 2)
    counts['already_present'] = 0
    counts['unresolved'] = 2
    assert 'No apps were opened' in _headline(counts, 2)
    counts['unresolved'] = 0
    counts['refused'] = 2
    assert 'No apps were opened' in _headline(counts, 2)
    counts['launch_requested'] = 1
    counts['launcher_accepted'] = 1
    assert 'does not prove' in _headline(counts, 2)


def test_preview_all_present_says_already_open_and_records_no_launch(monkeypatch):
    import starforge_workbench.favorite_apps_ui as ui
    class Present(Restorer):
        def preview(self, names):
            return {'selection': names, 'token': 'synthetic-token', 'items': [
                {'name': name, 'action': 'preserve', 'can_open_anyway': False,
                 'evidence': 'Verified open'} for name in names]}
    monkeypatch.setattr(ui, 'load_config', lambda _: [{'name': 'Editor'}])
    monkeypatch.setattr(ui.shutil, 'which', lambda _: '/usr/bin/zenity')
    answers = iter([(0, 'Editor\n'), (0, ''), (1, '')])
    shown = []
    def dialog(*args):
        shown.append(args)
        code, output = next(answers)
        return SimpleNamespace(returncode=code, stdout=output)
    monkeypatch.setattr(ui, '_dialog', dialog)
    restorer = Present()
    assert run_gui(restorer) == 0
    assert restorer.applied == []
    assert restorer.attempts[0]['phase'] == 'no_launch'
    assert restorer.attempts[0]['preview_counts']['preserve'] == 1
    assert any('already open' in value for value in shown[1])


def test_preview_all_refused_says_none_opened_and_records_reason(monkeypatch):
    import starforge_workbench.favorite_apps_ui as ui
    class Refused(Restorer):
        def preview(self, names):
            return {'selection': names, 'token': 'synthetic-token', 'items': [
                {'name': name, 'action': 'refuse', 'can_open_anyway': False,
                 'evidence': 'Desktop entry changed'} for name in names]}
    monkeypatch.setattr(ui, 'load_config', lambda _: [{'name': 'Editor'}])
    monkeypatch.setattr(ui.shutil, 'which', lambda _: '/usr/bin/zenity')
    answers = iter([(0, 'Editor\n'), (0, ''), (1, '')])
    shown = []
    def dialog(*args):
        shown.append(args)
        code, output = next(answers)
        return SimpleNamespace(returncode=code, stdout=output)
    monkeypatch.setattr(ui, '_dialog', dialog)
    restorer = Refused()
    assert run_gui(restorer) == 0
    assert restorer.applied == []
    assert restorer.attempts[0]['phase'] == 'no_launch'
    assert restorer.attempts[0]['preview_counts']['refuse'] == 1
    assert any('No apps were opened' in value for value in shown[1])


def test_apply_identity_change_records_error_before_warning(monkeypatch):
    import starforge_workbench.favorite_apps_ui as ui
    class Changed(Restorer):
        def apply(self, *args, **kwargs):
            raise ValueError('Selection or desktop identity changed; preview again')
    monkeypatch.setattr(ui, 'load_config', lambda _: [{'name': 'Editor'}])
    monkeypatch.setattr(ui.shutil, 'which', lambda _: '/usr/bin/zenity')
    answers = iter([(0, 'Editor\n'), (0, ''), (0, '')])
    monkeypatch.setattr(ui, '_dialog', lambda *args: SimpleNamespace(
        returncode=(answer := next(answers))[0], stdout=answer[1]))
    restorer = Changed()
    assert run_gui(restorer) == 1
    assert restorer.attempts[-1]['phase'] == 'error'
    assert restorer.attempts[-1]['failure_reason'] == 'identity_changed'
    assert restorer.attempts[-1]['result_counts']['launch_requested'] == 0


def test_attempt_audit_failure_after_launch_is_visible(monkeypatch):
    import starforge_workbench.favorite_apps_ui as ui
    class AuditFails(Restorer):
        def record_attempt(self, **event):
            if event['phase'] == 'completed':
                raise ValueError('Cannot save private attempt history')
            super().record_attempt(**event)
    monkeypatch.setattr(ui, 'load_config', lambda _: [{'name': 'Editor'}])
    monkeypatch.setattr(ui.shutil, 'which', lambda _: '/usr/bin/zenity')
    answers = iter([(0, 'Editor\n'), (0, ''), (0, '')])
    shown = []
    def dialog(*args):
        shown.append(args)
        code, output = next(answers)
        return SimpleNamespace(returncode=code, stdout=output)
    monkeypatch.setattr(ui, '_dialog', dialog)
    restorer = AuditFails()
    assert run_gui(restorer) == 1
    assert restorer.applied
    assert any('attempt summary could not be saved' in value for value in shown[-1])


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
