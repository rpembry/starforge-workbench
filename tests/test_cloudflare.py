import json
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from workbench.auth import CloudflareAuth
from workbench.client import client as api_client
from workbench.main import create_app
from workbench.repository import SQLiteRepository


@pytest.fixture
def access(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwks = SimpleNamespace(get_signing_key_from_jwt=lambda _: SimpleNamespace(key=key.public_key()))
    auth = CloudflareAuth('https://team.cloudflareaccess.com', 'audience', ['owner@example.com'], {'operator.access': 'operator', 'collector.access': 'collector'}, jwks=jwks)
    with TestClient(create_app(SQLiteRepository(tmp_path/'state'/'data.sqlite'), auth)) as api:
        yield api, key


def signed(key, **overrides):
    claims = {'iss': 'https://team.cloudflareaccess.com', 'aud': ['audience'], 'iat': int(time.time()), 'exp': int(time.time())+600, 'email': 'owner@example.com'}
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm='RS256', headers={'kid': 'fixture'})


def test_signed_browser_and_machine_roles(access):
    api, key = access
    api.headers['Cf-Access-Jwt-Assertion'] = signed(key)
    assert api.get('/').status_code == 200
    assert api.get('/ui/dashboard').status_code == 200
    assert api.post('/ui/actions', data={'title': 'A human action'}, headers={'Origin': 'http://testserver'}).status_code == 200
    assert api.get('/api/dashboard').json()['next'][0]['title'] == 'A human action'
    api.headers['Cf-Access-Jwt-Assertion'] = signed(key, common_name='collector.access')
    assert api.get('/api/dashboard').status_code == 403
    assert api.post('/api/actions', json={'title': 'Not committed', 'status': 'accepted'}).status_code == 403
    assert api.post('/api/events', json={'source': 'fixture', 'source_id': 'one', 'summary': 'Observed'}).status_code == 201
    api.headers['Cf-Access-Jwt-Assertion'] = signed(key, common_name='operator.access', email=None)
    assert api.get('/api/dashboard').status_code == 200


@pytest.mark.parametrize('claims', [{'aud': ['wrong']}, {'iss': 'https://evil.cloudflareaccess.com'}, {'exp': 1}, {'iat': int(time.time())+3600}])
def test_jwt_claim_checks(access, claims):
    api, key = access
    api.headers['Cf-Access-Jwt-Assertion'] = signed(key, **claims)
    assert api.get('/api/dashboard').status_code == 401


def test_spoofed_email_wrong_signature_and_unknown_identities(access):
    api, key = access
    assert api.get('/', headers={'Cf-Access-Authenticated-User-Email': 'owner@example.com', 'Authorization': 'Bearer operator-fixture'}).status_code == 401
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert api.get('/', headers={'Cf-Access-Jwt-Assertion': signed(other)}).status_code == 401
    for fields in [{'email': 'other@example.com'}, {'common_name': 'unknown.access'}]:
        assert api.get('/', headers={'Cf-Access-Jwt-Assertion': signed(key, **fields)}).status_code == 403


def test_browser_write_csrf_and_completion(access):
    api, key = access
    api.headers['Cf-Access-Jwt-Assertion'] = signed(key)
    assert api.post('/ui/actions', data={'title': 'bad'}).status_code == 403
    assert api.post('/ui/actions', data={'title': 'bad'}, headers={'Origin': 'https://evil.example'}).status_code == 403
    api.headers['Origin'] = 'http://testserver'
    assert api.post('/ui/actions', data={'title': '<script>x</script>'}).status_code == 200
    a = api.get('/api/actions').json()['items'][0]
    assert '&lt;script&gt;' in api.get('/ui/dashboard').text
    assert api.post('/ui/actions/'+a['id']+'/complete', data={'version': a['version']}).status_code == 200
    assert not api.get('/api/dashboard').json()['next']
    assert api.get('/assets/htmx.min.js').status_code == 200


def test_cloudflare_client_is_origin_bound_and_uses_distinct_roles(tmp_path):
    p=tmp_path/'client.json'
    p.write_text(json.dumps({'url':'https://workbench.example.com', 'auth_type':'cloudflare',
        'operator':{'client_id':'operator.access','client_secret':'fixture-operator'},
        'collector':{'client_id':'collector.access','client_secret':'fixture-collector'}}))
    p.chmod(0o600)
    with api_client(None,p,'collector') as api:
        assert api.headers['CF-Access-Client-Id']=='collector.access'
        assert 'Authorization' not in api.headers
    for url in ['https://evil.example', 'http://127.0.0.1:8027']:
        with pytest.raises(ValueError):api_client(url,p)
    p.chmod(0o644)
    with pytest.raises(ValueError):api_client(None,p)
