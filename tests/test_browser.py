from pathlib import Path

import pytest

from starforge_workbench.browser import add_entry, entries, load_config, refresh, remove_entry, save_config, update_entry


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
