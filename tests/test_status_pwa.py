"""PWA shell is static and operator gated; sensitive reads stay network-only."""
import json

from fastapi.testclient import TestClient

from workbench.auth import Auth
from workbench.main import create_app
from workbench.repository import SQLiteRepository
from workbench.settings import PersonalSettings


def test_static_shell_manifest_scope_and_private_config(tmp_path):
    app = create_app(SQLiteRepository(tmp_path / 'state' / 'db'),
        Auth({'operator': 'o' * 32, 'collector': 'c' * 32}),
        settings=PersonalSettings(human_name='Private Example', reporting_timezone='America/Indiana/Indianapolis'))
    api = TestClient(app)
    paths = ['/status', '/status/', '/status/manifest.webmanifest', '/status/sw.js',
             '/status/icon-192.png', '/status/icon-512.png', '/api/status/config']
    for path in paths:
        assert api.get(path).status_code == 401
        assert api.get(path, headers={'Authorization': 'Bearer ' + 'c' * 32}).status_code == 403
    headers = {'Authorization': 'Bearer ' + 'o' * 32}
    shell = api.get('/status/', headers=headers)
    assert shell.status_code == 200
    assert shell.headers['cache-control'] == 'no-store'
    assert 'manifest.webmanifest' in shell.text
    assert 'Private Example' not in shell.text
    assert 'America/Indiana/Indianapolis' not in shell.text
    assert 'operator' not in shell.text
    assert api.get('/status', headers=headers).content == shell.content
    manifest = api.get('/status/manifest.webmanifest', headers=headers)
    assert manifest.status_code == 200
    assert manifest.headers['content-type'].startswith('application/manifest+json')
    data = json.loads(manifest.content)
    assert data['start_url'] == data['scope'] == '/status/'
    assert data['display'] == 'standalone'
    assert {icon['sizes'] for icon in data['icons']} == {'192x192', '512x512'}
    for size in (192, 512):
        icon = api.get(f'/status/icon-{size}.png', headers=headers)
        assert icon.status_code == 200 and icon.content.startswith(b'\x89PNG\r\n\x1a\n')
    worker = api.get('/status/sw.js', headers=headers)
    assert worker.status_code == 200
    assert worker.headers['service-worker-allowed'] == '/status/'
    assert worker.headers['content-type'].startswith('application/javascript')
    assert '/api/status' not in worker.text
    config = api.get('/api/status/config', headers=headers)
    assert config.status_code == 200
    assert config.json() == {'profile': 'operator', 'timezone': 'America/Indiana/Indianapolis'}
    assert config.headers['cache-control'] == 'no-store'
