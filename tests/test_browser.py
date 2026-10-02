from pathlib import Path

import pytest

from starforge_workbench import browser


def test_browser_workspace_crud_is_owned_by_workbench(tmp_path):
    config = tmp_path / 'browser-workspaces.yaml'
    browser.add_entry('Mail', 'https://mail.example.com/', path=config)
    browser.add_entry('Source', 'https://code.example.com/', path=config)
    browser.update_entry('Source', new_name='Code', url='https://code.example.com/project/', path=config)
    assert [item['name'] for item in browser.entries(path=config)] == ['Mail', 'Code']
    assert config.stat().st_mode & 0o077 == 0
    browser.remove_entry('Mail', path=config)
    assert browser.entries(path=config) == [
        {'name': 'Code', 'url': 'https://code.example.com/project/', 'match': 'origin'}]


def test_browser_workspace_rejects_credentials_and_duplicate_names(tmp_path):
    config = tmp_path / 'browser-workspaces.yaml'
    with pytest.raises(ValueError):
        browser.add_entry('Bad', 'https://user:password@example.com', path=config)
    browser.add_entry('Site', 'https://example.com/', path=config)
    with pytest.raises(ValueError):
        browser.add_entry('site', 'https://other.example.com/', path=config)


def test_load_missing_config_has_empty_default_workspace(tmp_path):
    assert browser.load_config(tmp_path / 'missing.yaml') == {'version': 1, 'workspaces': {'default': []}}


class FakeChrome:
    def __init__(self, monkeypatch, tmp_path, tabs):
        self.connection = browser.BrowserConnection(9222, 'ws://127.0.0.1:9222/devtools/browser/test', tmp_path)
        self.tabs = list(tabs)
        self.launched = []
        self.closed = []
        self.on_launch = None
        monkeypatch.setattr(browser, '_connection', self.connect)
        monkeypatch.setattr(browser, '_tabs', self.read)
        monkeypatch.setattr(browser, '_launch_connected', self.launch)
        monkeypatch.setattr(browser, '_close_tab', self.close)

    def connect(self, port, profile):
        if port != 9222 or Path(profile) != self.connection.profile:
            raise browser.ChromeControlUnavailable('wrong Chrome profile')
        return self.connection

    def read(self, connection):
        assert connection == self.connection
        return [tab.copy() for tab in self.tabs]

    def launch(self, connection, flag, urls):
        assert connection == self.connection
        self.launched.append((flag, urls))
        if self.on_launch:
            self.on_launch(urls)
        else:
            for url in urls:
                self.tabs.append({'id': f'new-{len(self.tabs)}', 'url': url, 'type': 'page'})

    def close(self, target_id, connection):
        assert connection == self.connection
        self.closed.append(target_id)
        self.tabs = [tab for tab in self.tabs if tab['id'] != target_id]


def _config(tmp_path, *urls):
    config = tmp_path / 'browser-workspaces.yaml'
    for index, url in enumerate(urls):
        browser.add_entry(f'Site {index}', url, path=config, match='url')
    return config


def test_refresh_rechecks_and_does_not_duplicate_on_second_run(tmp_path, monkeypatch):
    config = _config(tmp_path, 'https://docs.example.com/new', 'https://other.example.com/')
    chrome = FakeChrome(monkeypatch, tmp_path, [
        {'id': 'existing', 'type': 'page', 'url': 'https://docs.example.com/new'},
        {'id': 'unrelated', 'type': 'page', 'url': 'https://unrelated.example.com/'},
    ])
    first = browser.refresh(path=config, profile=tmp_path)
    second = browser.refresh(path=config, profile=tmp_path)
    assert first['status'] == second['status'] == 'verified'
    assert first['requested'] == ['https://other.example.com/']
    assert second['requested'] == []
    assert chrome.launched == [('--new-tab', ['https://other.example.com/'])]
    assert any(tab['id'] == 'unrelated' for tab in chrome.tabs)


def test_wrong_or_missing_profile_fails_before_writes(tmp_path, monkeypatch):
    config = _config(tmp_path, 'https://site.example.com/')
    chrome = FakeChrome(monkeypatch, tmp_path, [])
    with pytest.raises(browser.ChromeControlUnavailable, match='wrong Chrome profile'):
        browser.refresh(path=config, profile=tmp_path / 'other')
    assert chrome.launched == []


def test_connection_identity_rejects_wrong_listener_profile(tmp_path, monkeypatch):
    profile = tmp_path / 'intended'
    profile.mkdir()
    other = tmp_path / 'other'
    other.mkdir()
    monkeypatch.setattr(browser, '_json_endpoint', lambda port, endpoint: {
        'webSocketDebuggerUrl': 'ws://127.0.0.1:9222/devtools/browser/one'})
    monkeypatch.setattr(browser, '_owner_profile', lambda port: other)
    with pytest.raises(browser.ChromeControlUnavailable, match='different Chrome profile'):
        browser._connection(9222, profile)


def test_listener_owner_accepts_single_space_separated_cmdline(tmp_path, monkeypatch):
    profile = tmp_path / 'profile'
    profile.mkdir()
    proc = tmp_path / 'proc'
    fd = proc / '123' / 'fd'
    fd.mkdir(parents=True)
    (fd / '8').symlink_to('socket:[42]')
    command = (f'/opt/google/chrome/chrome --no-first-run '
               f'--remote-debugging-address=127.0.0.1 --remote-debugging-port=33619 '
               f'--user-data-dir={profile} http://127.0.0.1:12345/source ')
    (proc / '123' / 'cmdline').write_bytes(command.encode())
    monkeypatch.setattr(browser, '_listener_inodes', lambda port: {'42'})
    assert browser._owner_profile(33619, proc) == profile
    (proc / '123' / 'cmdline').write_bytes(b'\0'.join(part.encode() for part in [
        '/opt/google/chrome/chrome', '--remote-debugging-port=33619', f'--user-data-dir={profile}']) + b'\0')
    assert browser._owner_profile(33619, proc) == profile


def test_listener_owner_rejects_wrong_or_ambiguous_single_field_identity(tmp_path, monkeypatch):
    profile = tmp_path / 'profile'
    profile.mkdir()
    proc = tmp_path / 'proc'
    fd = proc / '123' / 'fd'
    fd.mkdir(parents=True)
    (fd / '8').symlink_to('socket:[42]')
    cmdline = proc / '123' / 'cmdline'
    monkeypatch.setattr(browser, '_listener_inodes', lambda port: {'42'})
    valid = f'/opt/google/chrome/chrome --remote-debugging-port=33619 --user-data-dir={profile} '
    for command in (
        valid.replace('/chrome/chrome ', '/chrome/not-chrome '),
        valid.replace('port=33619', 'port=33620'),
        valid.replace(f'--user-data-dir={profile}', ''),
        valid + f'--user-data-dir={profile}',
        valid + '--remote-debugging-port=33620',
        valid.replace(f'--user-data-dir={profile}', '--user-data-dir=relative-profile'),
        valid + '"unterminated',
    ):
        cmdline.write_bytes(command.encode())
        with pytest.raises(browser.ChromeControlUnavailable, match='Cannot verify'):
            browser._owner_profile(33619, proc)
    cmdline.write_bytes(valid.encode())
    second_fd = proc / '456' / 'fd'
    second_fd.mkdir(parents=True)
    (second_fd / '9').symlink_to('socket:[42]')
    (proc / '456' / 'cmdline').write_bytes(valid.encode())
    with pytest.raises(browser.ChromeControlUnavailable, match='Cannot verify'):
        browser._owner_profile(33619, proc)
    monkeypatch.setattr(browser, '_listener_inodes', lambda port: set())
    with pytest.raises(browser.ChromeControlUnavailable, match='No unique'):
        browser._owner_profile(33619, proc)


def test_launch_targets_the_verified_profile(tmp_path, monkeypatch):
    connection = browser.BrowserConnection(9222, 'ws://127.0.0.1:9222/devtools/browser/test', tmp_path)
    monkeypatch.setattr(browser, '_connection', lambda port, profile: connection)
    monkeypatch.setattr(browser, '_executable', lambda: '/usr/bin/google-chrome')
    launched = []
    monkeypatch.setattr(browser.subprocess, 'Popen', lambda args, **kwargs: launched.append(args))
    browser._launch_connected(connection, '--new-window', ['https://site.example.com/'])
    assert launched == [[
        '/usr/bin/google-chrome', f'--user-data-dir={tmp_path}', '--new-window', 'https://site.example.com/']]


def test_tab_read_rejects_browser_restart(tmp_path, monkeypatch):
    connection = browser.BrowserConnection(9222, 'ws://127.0.0.1:9222/devtools/browser/one', tmp_path)
    replacement = browser.BrowserConnection(9222, 'ws://127.0.0.1:9222/devtools/browser/two', tmp_path)
    monkeypatch.setattr(browser, '_connection', lambda port, profile: replacement)
    with pytest.raises(browser.ChromeControlUnavailable, match='changed'):
        browser._tabs(connection)


def test_destination_verification_waits_for_delayed_target_and_reports_missing(tmp_path, monkeypatch):
    connection = browser.BrowserConnection(9222, 'ws://127.0.0.1:9222/devtools/browser/test', tmp_path)
    calls = []
    def tabs(_):
        calls.append(1)
        return [] if len(calls) == 1 else [{'id': 'new', 'url': 'https://site.example.com/a'}]
    monkeypatch.setattr(browser, '_tabs', tabs)
    found, missing = browser._verify_new_tabs(connection, [], ['https://site.example.com/a'])
    assert [item['id'] for item in found] == ['new']
    assert missing == []
    monkeypatch.setattr(browser, '_tabs', lambda _: [])
    found, missing = browser._verify_new_tabs(connection, [], ['https://site.example.com/b'], timeout=0)
    assert found == []
    assert missing == ['https://site.example.com/b']


def test_move_rejects_changed_preview_or_navigation_before_launch(tmp_path, monkeypatch):
    config = _config(tmp_path, 'https://site.example.com/a')
    chrome = FakeChrome(monkeypatch, tmp_path, [{'id': 'source', 'type': 'page', 'url': 'https://site.example.com/a'}])
    preview = browser.organize(path=config, profile=tmp_path)
    chrome.tabs[0]['url'] = 'https://site.example.com/b'
    with pytest.raises(ValueError, match='target set changed'):
        browser.organize(path=config, profile=tmp_path, apply=True, action='move', expect=preview['expect'])
    assert chrome.launched == chrome.closed == []


def test_move_rejects_browser_restart_even_if_tab_ids_reappear(tmp_path, monkeypatch):
    config = _config(tmp_path, 'https://site.example.com/a')
    chrome = FakeChrome(monkeypatch, tmp_path, [{'id': 'source', 'type': 'page', 'url': 'https://site.example.com/a'}])
    preview = browser.organize(path=config, profile=tmp_path)
    chrome.connection = browser.BrowserConnection(9222, 'ws://127.0.0.1:9222/devtools/browser/restarted', tmp_path)
    with pytest.raises(ValueError, match='target set changed'):
        browser.organize(path=config, profile=tmp_path, apply=True, action='move', expect=preview['expect'])
    assert chrome.closed == chrome.launched == []


def test_move_preserves_source_that_navigates_after_copy(tmp_path, monkeypatch):
    config = _config(tmp_path, 'https://site.example.com/a')
    chrome = FakeChrome(monkeypatch, tmp_path, [{'id': 'source', 'type': 'page', 'url': 'https://site.example.com/a'}])
    preview = browser.organize(path=config, profile=tmp_path)
    def launch(urls):
        chrome.tabs.append({'id': 'new', 'type': 'page', 'url': urls[0]})
        chrome.tabs[0]['url'] = 'https://site.example.com/changed'
    chrome.on_launch = launch
    result = browser.organize(path=config, profile=tmp_path, apply=True, action='move', expect=preview['expect'])
    assert result['status'] == 'partial'
    assert result['preserved'] == ['source']
    assert chrome.closed == []


def test_move_verifies_each_destination_and_preserves_unrelated_or_partial_sources(tmp_path, monkeypatch):
    config = _config(tmp_path, 'https://site.example.com/a', 'https://site.example.com/b')
    chrome = FakeChrome(monkeypatch, tmp_path, [
        {'id': 'one', 'type': 'page', 'url': 'https://site.example.com/a'},
        {'id': 'two', 'type': 'page', 'url': 'https://site.example.com/b'},
        {'id': 'unrelated', 'type': 'page', 'url': 'https://unrelated.example.com/'},
    ])
    chrome.on_launch = lambda urls: chrome.tabs.append({'id': 'new-one', 'type': 'page', 'url': urls[0]})
    preview = browser.organize(path=config, profile=tmp_path)
    monkeypatch.setattr(browser, '_verify_new_tabs', lambda connection, before, urls: (
        [{'id': 'new-one', 'url': urls[0]}], [urls[1]]))
    result = browser.organize(path=config, profile=tmp_path, apply=True, action='move', expect=preview['expect'])
    assert result['status'] == 'partial'
    assert result['requested'] == ['https://site.example.com/a', 'https://site.example.com/b']
    assert result['closed'] == chrome.closed == ['one']
    assert result['preserved'] == ['two']
    assert {tab['id'] for tab in chrome.tabs} == {'two', 'unrelated', 'new-one'}


def test_move_preserves_sources_on_connection_loss_after_launch(tmp_path, monkeypatch):
    config = _config(tmp_path, 'https://site.example.com/a')
    chrome = FakeChrome(monkeypatch, tmp_path, [{'id': 'source', 'type': 'page', 'url': 'https://site.example.com/a'}])
    preview = browser.organize(path=config, profile=tmp_path)
    def disconnected(*args):
        raise browser.ChromeControlUnavailable('connection lost')
    monkeypatch.setattr(browser, '_verify_new_tabs', disconnected)
    result = browser.organize(path=config, profile=tmp_path, apply=True, action='move', expect=preview['expect'])
    assert result['status'] == 'uncertain'
    assert result['preserved'] == ['source']
    assert chrome.closed == []


def test_copy_reports_partial_creation_and_keeps_all_sources(tmp_path, monkeypatch):
    config = _config(tmp_path, 'https://site.example.com/a', 'https://site.example.com/b')
    chrome = FakeChrome(monkeypatch, tmp_path, [
        {'id': 'one', 'type': 'page', 'url': 'https://site.example.com/a'},
        {'id': 'two', 'type': 'page', 'url': 'https://site.example.com/b'},
    ])
    chrome.on_launch = lambda urls: chrome.tabs.append({'id': 'new-one', 'type': 'page', 'url': urls[0]})
    preview = browser.organize(path=config, profile=tmp_path)
    monkeypatch.setattr(browser, '_verify_new_tabs', lambda connection, before, urls: (
        [{'id': 'new-one', 'url': urls[0]}], [urls[1]]))
    result = browser.organize(path=config, profile=tmp_path, apply=True, expect=preview['expect'])
    assert result['status'] == 'partial'
    assert result['preserved'] == ['one', 'two']
    assert result['verified'][0]['destination_id'] == 'new-one'
    assert result['partial'][0]['source_id'] == 'two'
    assert chrome.closed == []
