"""Synthetic exact-identity desktop restoration; no normal applications launch."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
import yaml

from starforge_workbench.favorite_apps import FavoriteRestore, X11Desktop, load_config, resolve_entry


class Desktop:
    def __init__(self):
        self.states = {}
        self.launches = []
        self.launch_result = {}
        self.started = None
        self.release = None

    def observe(self, favorite, entry):
        state = self.states.get(favorite['name'], 'absent')
        return state, 'Synthetic ' + state + ' evidence'

    def launch(self, desktop_id):
        self.launches.append(desktop_id)
        if self.started:
            self.started.set()
            self.release.wait(timeout=5)
        return self.launch_result.get(desktop_id, True)


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / 'private'
    root.mkdir(mode=0o700)
    applications = tmp_path / 'applications'
    applications.mkdir()
    executable = tmp_path / 'synthetic-app'
    executable.write_text('fixture only')
    executable.chmod(0o755)
    favorites = []
    for name, kind in [('Editor', 'native'), ('Notes', 'pwa'),
                       ('Terminal', 'terminal'), ('Workbench', 'workbench')]:
        desktop_id = name.lower() + '.desktop'
        row = {'name': name, 'kind': kind, 'desktop_id': desktop_id,
               'wm_class': 'synthetic-' + name.lower(), 'executable': str(executable)}
        flags = ''
        if kind == 'pwa':
            row.update(profile='Profile 2', app_id='synthetic-app-id')
            flags = ' --profile-directory="Profile 2" --app-id=synthetic-app-id'
        (applications / desktop_id).write_text(
            '[Desktop Entry]\nType=Application\nName=Synthetic ' + name +
            '\nExec=' + str(executable) + flags +
            '\nStartupWMClass=' + row['wm_class'] + '\n')
        favorites.append(row)
    config = root / 'favorite-apps.yaml'
    config.write_text(yaml.safe_dump({'version': 1, 'favorites': favorites}))
    config.chmod(0o600)
    desktop = Desktop()
    restore = FavoriteRestore(config, [applications], desktop, sleeper=lambda _: None)
    return restore, desktop, config, applications, favorites


def test_preview_is_read_only_and_resolves_all_variants(setup):
    restore, desktop, config, _, _ = setup
    before = config.read_bytes()
    result = restore.preview(['Editor', 'Notes', 'Terminal', 'Workbench'])
    assert [item['action'] for item in result['items']] == ['launch'] * 4
    assert [item['kind'] for item in result['items']] == ['native', 'pwa', 'terminal', 'workbench']
    assert desktop.launches == []
    assert config.read_bytes() == before
    assert not restore.state_path.exists() and not restore.lock_path.exists()


def test_present_is_preserved_and_delayed_launch_stays_uncertain(setup):
    restore, desktop, _, _, _ = setup
    desktop.states['Editor'] = 'present'
    preview = restore.preview(['Editor', 'Notes'])
    assert [item['action'] for item in preview['items']] == ['preserve', 'launch']
    first = restore.apply(preview['selection'], preview['token'])
    assert [item['result'] for item in first['items']] == ['already_present', 'uncertain']
    assert desktop.launches == ['notes.desktop']
    assert restore.preview(['Notes'])['items'][0]['action'] == 'skip'
    again = restore.apply(preview['selection'], preview['token'])
    assert again['items'][1]['result'] == 'uncertain'
    assert desktop.launches == ['notes.desktop']
    desktop.states['Notes'] = 'present'
    ready = restore.apply(['Notes'], restore.preview(['Notes'])['token'])
    assert ready['items'][0]['result'] == 'already_present'
    assert json.loads(restore.state_path.read_text()) == {}


def test_ready_and_partial_failure_are_reported_per_target(setup):
    restore, desktop, _, applications, _ = setup
    original_launch = desktop.launch

    def launch(desktop_id):
        accepted = original_launch(desktop_id)
        if desktop_id == 'editor.desktop':
            desktop.states['Editor'] = 'present'
        return accepted

    desktop.launch = launch
    desktop.launch_result['notes.desktop'] = False
    preview = restore.preview(['Editor', 'Notes', 'Terminal'])
    (applications / 'terminal.desktop').unlink()
    with pytest.raises(ValueError, match='preview again'):
        restore.apply(preview['selection'], preview['token'])
    assert desktop.launches == []
    preview = restore.preview(['Editor', 'Notes', 'Terminal'])
    result = restore.apply(preview['selection'], preview['token'])
    assert [item['result'] for item in result['items']] == ['verified_ready', 'uncertain', 'refused']
    assert [item['launch_requested'] for item in result['items']] == [True, True, False]
    assert [item['launcher_accepted'] for item in result['items']] == [True, False, None]
    assert desktop.launches == ['editor.desktop', 'notes.desktop']
    assert restore.preview(['Editor'])['items'][0]['action'] == 'preserve'


def test_changed_selection_entry_or_pwa_profile_rejected(setup):
    restore, desktop, config, applications, favorites = setup
    preview = restore.preview(['Editor'])
    with pytest.raises(ValueError, match='preview again'):
        restore.apply(['Notes'], preview['token'])
    entry = applications / 'editor.desktop'
    entry.write_text(entry.read_text() + 'Comment=changed\n')
    with pytest.raises(ValueError, match='preview again'):
        restore.apply(['Editor'], preview['token'])
    assert desktop.launches == []
    favorites[1]['profile'] = 'Profile 9'
    config.write_text(yaml.safe_dump({'version': 1, 'favorites': favorites}))
    assert restore.preview(['Notes'])['items'][0]['action'] == 'refuse'


def test_missing_collision_and_duplicate_label_fail_closed(setup, tmp_path):
    restore, desktop, config, applications, favorites = setup
    (applications / 'editor.desktop').unlink()
    assert restore.preview(['Editor'])['items'][0]['action'] == 'refuse'
    (applications / 'editor.desktop').write_text('[Desktop Entry]\nType=Application\nExec=' + favorites[0]['executable'])
    second = tmp_path / 'other-applications'
    second.mkdir()
    (second / 'editor.desktop').write_bytes((applications / 'editor.desktop').read_bytes())
    restore.directories.append(second)
    assert restore.preview(['Editor'])['items'][0]['action'] == 'refuse'
    favorites.append({**favorites[0], 'name': 'EDITOR'})
    config.write_text(yaml.safe_dump({'version': 1, 'favorites': favorites}))
    with pytest.raises(ValueError, match='unique'):
        load_config(config)
    assert desktop.launches == []


def test_desktop_unavailable_and_uncertain_evidence_never_launch(setup):
    restore, desktop, _, _, _ = setup
    for state in ('unavailable', 'uncertain'):
        desktop.states['Editor'] = state
        preview = restore.preview(['Editor'])
        assert preview['items'][0]['action'] == 'refuse'
        result = restore.apply(['Editor'], preview['token'])
        assert result['items'][0]['result'] == 'refused'
    assert desktop.launches == []


def test_concurrent_apply_has_one_launch_request(setup):
    restore, desktop, _, _, _ = setup
    preview = restore.preview(['Editor'])
    desktop.started = threading.Event()
    desktop.release = threading.Event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(restore.apply, ['Editor'], preview['token'])
        assert desktop.started.wait(timeout=5)
        second = pool.submit(restore.apply, ['Editor'], preview['token'])
        desktop.release.set()
        results = [first.result(timeout=5), second.result(timeout=5)]
    assert len(desktop.launches) == 1
    assert all(item['items'][0]['result'] == 'uncertain' for item in results)


def test_changed_pending_identity_refuses_relaunch(setup):
    restore, desktop, _, applications, _ = setup
    preview = restore.preview(['Editor'])
    restore.apply(['Editor'], preview['token'])
    entry = applications / 'editor.desktop'
    entry.write_text(entry.read_text() + 'Comment=changed\n')
    new_preview = restore.preview(['Editor'])
    assert new_preview['items'][0]['action'] == 'refuse'
    assert restore.apply(['Editor'], new_preview['token'])['items'][0]['result'] == 'refused'
    assert desktop.launches == ['editor.desktop']


def test_renaming_favorite_cannot_bypass_pending_launch(setup):
    restore, desktop, config, _, favorites = setup
    preview = restore.preview(['Editor'])
    restore.apply(['Editor'], preview['token'])
    favorites[0]['name'] = 'Renamed Editor'
    config.write_text(yaml.safe_dump({'version': 1, 'favorites': favorites}))
    renamed = restore.preview(['Renamed Editor'])
    assert renamed['items'][0]['action'] == 'skip'
    assert restore.apply(['Renamed Editor'], renamed['token'])['items'][0]['result'] == 'uncertain'
    assert desktop.launches == ['editor.desktop']


def test_pwa_entry_flags_and_private_config_required(setup):
    restore, _, config, applications, favorites = setup
    entry = applications / 'notes.desktop'
    entry.write_text(entry.read_text().replace('Profile 2', 'Profile 1'))
    assert restore.preview(['Notes'])['items'][0]['action'] == 'refuse'
    config.chmod(0o644)
    with pytest.raises(ValueError, match='mode 0600'):
        load_config(config)
    config.chmod(0o600)
    favorites[1]['app_id'] = ''
    config.write_text(yaml.safe_dump({'version': 1, 'favorites': favorites}))
    with pytest.raises(ValueError, match='PWA requires'):
        load_config(config)


def test_duplicate_desktop_id_is_rejected(setup):
    _, _, config, _, favorites = setup
    favorites[1]['desktop_id'] = favorites[0]['desktop_id']
    config.write_text(yaml.safe_dump({'version': 1, 'favorites': favorites}))
    with pytest.raises(ValueError, match='desktop IDs must be unique'):
        load_config(config)


def test_x11_pwa_window_and_process_identity_fixture(setup, tmp_path, monkeypatch):
    _, _, _, applications, favorites = setup
    favorite = favorites[1]
    entry = resolve_entry(favorite, [applications])
    proc = tmp_path / 'proc'
    proc.mkdir()
    owner = proc / '100'
    owner.mkdir()
    owner.joinpath('exe').symlink_to(entry['executable'])
    owner.joinpath('cmdline').write_bytes(b'synthetic-app\0--profile-directory=Profile 1\0--app-id=synthetic-app-id\0')
    windows = '0x01  0  100  0  0  600  400  synthetic-notes  host  Synthetic Notes\n'
    monkeypatch.setattr('starforge_workbench.favorite_apps.shutil.which', lambda _: '/usr/bin/synthetic-tool')
    monkeypatch.setattr('starforge_workbench.favorite_apps.subprocess.run',
                        lambda *args, **kwargs: SimpleNamespace(stdout=windows))
    desktop = X11Desktop({'XDG_SESSION_TYPE': 'x11', 'DISPLAY': ':99'}, proc)
    assert desktop.observe(favorite, entry)[0] == 'uncertain'
    owner.joinpath('cmdline').write_bytes(b'synthetic-app\0--profile-directory=Profile 2\0--app-id=synthetic-app-id\0')
    assert desktop.observe(favorite, entry)[0] == 'present'
    windows = ''
    assert desktop.observe(favorite, entry)[0] == 'uncertain'
    owner.joinpath('cmdline').write_bytes(b'synthetic-app\0--profile-directory=Profile 1\0--app-id=synthetic-app-id\0')
    assert desktop.observe(favorite, entry)[0] == 'absent'
    assert X11Desktop({'XDG_SESSION_TYPE': 'wayland', 'DISPLAY': ':99'}, proc).observe(favorite, entry)[0] == 'unavailable'
