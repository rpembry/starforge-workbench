from pathlib import Path

import pytest

from starforge_workbench.browser import add_entry, entries, load_config, organize, refresh, remove_entry, save_config, update_entry


def test_browser_workspace_crud_is_owned_by_workbench(tmp_path):
    config = tmp_path / 'browser-workspaces.yaml'

    add_entry('Gmail', 'https://mail.google.com/', path=config)
    add_entry('GitHub', 'https://github.com/', path=config)
    update_entry('GitHub', new_name='Source', url='https://github.com/AleTechnologies/', path=config)
    assert [item['name'] for item in entries(path=config)] == ['Gmail', 'Source']
    assert config.stat().st_mode & 0o077 == 0

    remove_entry('Gmail', path=config)
    assert entries(path=config) == [
        {'name': 'Source', 'url': 'https://github.com/AleTechnologies/', 'match': 'origin'}
    ]


def test_browser_workspace_rejects_credentials_and_duplicate_names(tmp_path):
    config = tmp_path / 'browser-workspaces.yaml'
    with pytest.raises(ValueError):
        add_entry('Bad', 'https://user:password@example.com', path=config)
    add_entry('Site', 'https://example.com/', path=config)
    with pytest.raises(ValueError):
        add_entry('site', 'https://other.example.com/', path=config)


def test_refresh_opens_only_missing_canonical_origins(tmp_path, monkeypatch):
    config = tmp_path / 'browser-workspaces.yaml'
    add_entry('Docs', 'https://docs.example.com/new', path=config)
    add_entry('Other', 'https://other.example.com/', path=config)
    monkeypatch.setattr('starforge_workbench.browser._tabs', lambda port: [{'type': 'page', 'url': 'https://docs.example.com/already'}])
    launched = []
    monkeypatch.setattr('starforge_workbench.browser._executable', lambda: '/usr/bin/google-chrome')
    monkeypatch.setattr('starforge_workbench.browser.subprocess.Popen', lambda args, **kwargs: launched.append(args))

    result = refresh(port=9222, path=config)

    assert result['opened'] == ['Other']
    assert launched == [['/usr/bin/google-chrome', '--new-tab', 'https://other.example.com/']]


def test_load_missing_config_has_empty_default_workspace(tmp_path):
    assert load_config(tmp_path / 'missing.yaml') == {'version': 1, 'workspaces': {'default': []}}


def test_organize_previews_only_matching_tabs_without_mutating_chrome(tmp_path, monkeypatch):
    config = tmp_path / 'browser-workspaces.yaml'
    add_entry('Docs', 'https://docs.example.com/start', path=config)
    monkeypatch.setattr('starforge_workbench.browser._tabs', lambda port: [
        {'id': 'selected', 'type': 'page', 'url': 'https://docs.example.com/current'},
        {'id': 'unrelated', 'type': 'page', 'url': 'https://elsewhere.example.com/'},
    ])
    launched = []
    monkeypatch.setattr('starforge_workbench.browser.subprocess.Popen', lambda args, **kwargs: launched.append(args))

    result = organize(port=9222, path=config)

    assert result == {
        'status': 'preview', 'workspace': 'default', 'action': 'copy', 'unmatched_tabs': 1,
        'selected': [{'id': 'selected', 'url': 'https://docs.example.com/current', 'entry': 'Docs', 'match': 'origin'}],
    }
    assert launched == []


def test_organize_move_opens_selected_tabs_then_closes_only_selected_originals(tmp_path, monkeypatch):
    config = tmp_path / 'browser-workspaces.yaml'
    add_entry('Docs', 'https://docs.example.com/start', path=config)
    tabs = [
        {'id': 'selected', 'type': 'page', 'url': 'https://docs.example.com/current'},
        {'id': 'unrelated', 'type': 'page', 'url': 'https://elsewhere.example.com/'},
    ]
    monkeypatch.setattr('starforge_workbench.browser._tabs', lambda port: list(tabs))
    monkeypatch.setattr('starforge_workbench.browser._executable', lambda: '/usr/bin/google-chrome')
    launched = []
    monkeypatch.setattr('starforge_workbench.browser.subprocess.Popen', lambda args, **kwargs: launched.append(args))

    def close(target_id, port):
        nonlocal tabs
        tabs = [tab for tab in tabs if tab['id'] != target_id]
    monkeypatch.setattr('starforge_workbench.browser._close_tab', close)

    result = organize(port=9222, path=config, apply=True, action='move')

    assert launched == [['/usr/bin/google-chrome', '--new-window', 'https://docs.example.com/current']]
    assert result['opened'] == ['https://docs.example.com/current']
    assert result['closed'] == ['selected']
    assert result['still_open'] == []
    assert result['close_errors'] == []
    assert result['verification'] == 'verified'
    assert tabs == [{'id': 'unrelated', 'type': 'page', 'url': 'https://elsewhere.example.com/'}]


def test_organize_move_reports_close_failure_without_touching_unrelated_tabs(tmp_path, monkeypatch):
    config = tmp_path / 'browser-workspaces.yaml'
    add_entry('Docs', 'https://docs.example.com/start', path=config)
    tabs = [
        {'id': 'selected', 'type': 'page', 'url': 'https://docs.example.com/current'},
        {'id': 'unrelated', 'type': 'page', 'url': 'https://elsewhere.example.com/'},
    ]
    monkeypatch.setattr('starforge_workbench.browser._tabs', lambda port: list(tabs))
    monkeypatch.setattr('starforge_workbench.browser._executable', lambda: '/usr/bin/google-chrome')
    monkeypatch.setattr('starforge_workbench.browser.subprocess.Popen', lambda *args, **kwargs: None)
    monkeypatch.setattr('starforge_workbench.browser._close_tab', lambda target_id, port: (_ for _ in ()).throw(ValueError('close failed')))

    result = organize(port=9222, path=config, apply=True, action='move')

    assert result['closed'] == []
    assert result['still_open'] == ['selected']
    assert result['close_errors'] == [{'id': 'selected', 'url': 'https://docs.example.com/current', 'reason': 'close failed'}]
    assert tabs[1]['id'] == 'unrelated'
