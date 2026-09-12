"""Small authenticated JSON client for the persistent Workbench conversation."""
import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .auth import Auth


def client(url=None, credentials_file=None, role='operator'):
    if url:
        parsed = urlsplit(url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment or (parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', 'localhost', '::1'})):
            raise ValueError('Use a credential-free HTTPS or loopback HTTP URL')
    credentials_file = credentials_file or os.environ.get('WB_CLIENT_CONFIG') or Path.home()/'.config/starforge-ai-workbench/client.json'
    path = Path(credentials_file)
    if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError('Client credentials must be owned by you with mode 0600')
    data = json.loads(path.read_text())
    url = url or data.get('url') or 'http://127.0.0.1:8027'
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('API URL must not contain credentials, query, or fragment')
    if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', 'localhost', '::1'}):
        raise ValueError('Use HTTPS or an explicit loopback HTTP endpoint')
    if role not in {'operator', 'collector'}:
        raise ValueError('Unknown credential role')
    if data.get('auth_type') == 'cloudflare':
        if parsed.scheme != 'https' or url.rstrip('/') != data['url'].rstrip('/'):
            raise ValueError('Cloudflare credentials are bound to their configured HTTPS origin')
        credentials = data[role]
        headers = {'CF-Access-Client-Id': credentials['client_id'], 'CF-Access-Client-Secret': credentials['client_secret']}
    else:
        headers = {'Authorization': 'Bearer '+Auth(data).credentials[role]}
    return httpx.Client(base_url=url.rstrip('/'), headers=headers, timeout=15, follow_redirects=False)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('method', choices=['GET', 'POST', 'PATCH'])
    p.add_argument('path', help='Relative /api/... path or /openapi.json')
    p.add_argument('--json-file', type=Path, help='JSON request file; omit to read stdin for writes')
    p.add_argument('--url', default=os.environ.get('WB_API_URL'))
    p.add_argument('--credentials-file', default=os.environ.get('WB_CREDENTIALS_FILE'))
    args = p.parse_args()
    if not args.path.startswith(('/api/', '/openapi.json')) or args.path.startswith('//'):
        p.error('Only relative Workbench API paths are allowed')
    data = None
    if args.method != 'GET':
        data = json.loads(args.json_file.read_text() if args.json_file else sys.stdin.read())
    with client(args.url, args.credentials_file) as api:
        response = api.request(args.method, args.path, json=data)
    print(json.dumps(response.json(), indent=2))
    if response.is_error:
        raise SystemExit(1)
