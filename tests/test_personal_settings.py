from datetime import datetime, timezone
import traceback

import pytest
from fastapi.testclient import TestClient

from workbench.auth import Auth
from workbench.main import create_app
from workbench.reports import event_time, standup_window
from workbench.repository import SQLiteRepository
from workbench.settings import DEFAULT_SETTINGS, PersonalSettings, load_settings


OPERATOR = 'operator-fixture-' + 'x' * 32
COLLECTOR = 'collector-fixture-' + 'y' * 32


def client(tmp_path, settings):
    app = create_app(SQLiteRepository(tmp_path/'state'/'workbench.sqlite'), Auth({'operator': OPERATOR, 'collector': COLLECTOR}), settings)
    result = TestClient(app)
    result.headers['Authorization'] = 'Bearer ' + OPERATOR
    return result


def test_defaults_remain_compatible_and_explicit_actors_are_preserved(tmp_path):
    assert load_settings() == DEFAULT_SETTINGS
    with client(tmp_path, DEFAULT_SETTINGS) as api:
        omitted = api.post('/api/actions', json={'title': 'Default actor'}).json()
        explicit = api.post('/api/actions', json={'title': 'Explicit actor', 'actor': 'Different person'}).json()
        provider = api.post('/api/actions', json={'title': 'Provider identity', 'execution_mode': 'agent', 'actor': 'codex'}).json()
    assert omitted['actor'] == 'Operator'
    assert explicit['actor'] == 'Different person'
    assert provider['actor'] == 'codex'


def test_external_settings_apply_per_app_to_api_and_browser_actions(tmp_path):
    settings_file = tmp_path/'settings.yaml'
    settings_file.write_text('human_name: Avery Example\nreporting_timezone: America/Los_Angeles\n')
    custom = load_settings(settings_file)
    with client(tmp_path/'custom', custom) as api:
        api_action = api.post('/api/actions', json={'title': 'API action'}).json()
        response = api.post('/ui/actions', data={'title': 'Browser action'}, headers={'Origin': 'http://testserver'})
        browser_action = next(item for item in api.get('/api/actions').json()['items'] if item['title'] == 'Browser action')
    with client(tmp_path/'default', DEFAULT_SETTINGS) as default_api:
        default_action = default_api.post('/api/actions', json={'title': 'Default app'}).json()
    assert response.status_code == 200
    assert api_action['actor'] == browser_action['actor'] == 'Avery Example'
    assert default_action['actor'] == 'Operator'


def test_custom_timezone_handles_dst_weekend_and_legacy_import_provenance(tmp_path):
    zone = PersonalSettings(reporting_timezone='America/Los_Angeles').zone
    start, end = standup_window(datetime(2026, 3, 9, 16, tzinfo=timezone.utc), zone)
    assert start.isoformat() == '2026-03-06T09:00:00-08:00'
    assert end.isoformat() == '2026-03-09T09:00:00-07:00'
    legacy = event_time({'id': 'legacy', 'occurred_at': '2026-03-06T00:00:00-05:00'}, {'legacy': {'occurred_at': '2026-03-06'}}, zone)
    assert legacy.tzinfo.key == 'America/New_York'
    with client(tmp_path, PersonalSettings(reporting_timezone='America/Los_Angeles')) as api:
        report = api.get('/api/reports/standup').json()
    assert report['timezone'] == 'America/Los_Angeles'


@pytest.mark.parametrize('contents, message', [
    ('human_name: ""\n', 'human_name'),
    ('reporting_timezone: Not/AZone\n', 'reporting_timezone'),
    ('reporting_timezone: ../PRIVATE_SENTINEL\n', 'reporting_timezone'),
    ('human_name: [PRIVATE_SENTINEL\n', 'settings YAML'),
])
def test_invalid_settings_are_safe_and_specific(tmp_path, contents, message):
    path = tmp_path/'settings.yaml'
    path.write_text(contents)
    with pytest.raises(RuntimeError, match=message) as excinfo:
        load_settings(path)
    rendered = ''.join(traceback.format_exception(excinfo.value))
    assert 'PRIVATE_SENTINEL' not in rendered
    assert contents.strip() not in rendered


def test_invalid_settings_encoding_is_not_rendered_in_traceback(tmp_path):
    path = tmp_path/'settings.yaml'
    path.write_bytes(b'\xffPRIVATE_SENTINEL')
    with pytest.raises(RuntimeError, match='decode') as excinfo:
        load_settings(path)
    assert 'PRIVATE_SENTINEL' not in ''.join(traceback.format_exception(excinfo.value))
