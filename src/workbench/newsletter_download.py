"""Bounded official attachment flow; the token never follows a redirect."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

import httpx

GRANT = 'todoist-files-origin-download.v1'
FILE_HOST = 'files.todoist.com'
CDN_HOSTS = frozenset({'todoist.b-cdn.net', 'd1ysz50cxb9zwl.cloudfront.net'})
MAX_BYTES = 2_000_000


class DownloadError(ValueError):
    """Fixed error codes, never remote bodies, URLs, headers or tokens."""


def validate_url(url: str, *, cdn: bool = False):
    if not isinstance(url, str) or len(url) > 8192 or not url.isascii():
        raise DownloadError('unsafe_attachment_url')
    if any(ord(c) <= 32 or ord(c) == 127 for c in url) or '\\' in url:
        raise DownloadError('unsafe_attachment_url')
    try:
        parsed = urlsplit(url)
    except ValueError:
        raise DownloadError('unsafe_attachment_url') from None
    hosts = CDN_HOSTS if cdn else {FILE_HOST}
    if parsed.scheme != 'https' or parsed.netloc not in hosts or parsed.fragment:
        raise DownloadError('unsafe_attachment_url')
    decoded = unquote(parsed.path)
    if (not decoded.startswith('/') or decoded.startswith('//') or '%' in decoded
            or '\\' in decoded or any(ord(c) < 32 or ord(c) == 127 for c in decoded)
            or any(part in {'.', '..'} for part in decoded.split('/'))):
        raise DownloadError('unsafe_attachment_path')
    if not cdn and (not re.fullmatch(r'/user_upload/(?:v2/)?[A-Za-z0-9_-]+/[^/]+\.html', decoded)
                    or parsed.query):
        raise DownloadError('unsafe_attachment_path')
    if cdn and not parsed.query:
        raise DownloadError('unsigned_cdn_url')
    if any(ord(c) < 32 or ord(c) == 127 for c in unquote(parsed.query)):
        raise DownloadError('unsafe_attachment_url')


def _response(client: httpx.Client, url: str, token: str | None):
    request = client.build_request('GET', url, headers={
        'Accept': 'text/html,application/octet-stream', 'Accept-Encoding': 'identity'})
    # Strip ambient client credentials/cookies too, including on mocked clients.
    request.headers.pop('Authorization', None)
    request.headers.pop('Cookie', None)
    if token is not None:
        request.headers['Authorization'] = 'Bearer ' + token
    return client.send(request, stream=True, follow_redirects=False, auth=None)


def read_html(client: httpx.Client, url: str, *, token: str | None = None) -> str:
    validate_url(url)
    response = _response(client, url, token)
    try:
        if response.status_code in {301,302,303,307,308}:
            if token is None:
                raise DownloadError('attachment_auth_required')
            target = response.headers.get('location', '')
            # Only one explicit hop to a known signed CDN URL is permitted.
            validate_url(target, cdn=True)
            response.close()
            response = _response(client, target, None)
        if response.status_code != 200:
            raise DownloadError('attachment_unavailable')
        if response.headers.get('content-encoding', 'identity').lower() != 'identity':
            raise DownloadError('encoded_attachment_rejected')
        length = response.headers.get('content-length')
        if length is not None and (not length.isdigit() or int(length) > MAX_BYTES):
            raise DownloadError('source_too_large')
        content_type = response.headers.get('content-type', '').split(';')[0].strip().lower()
        if content_type not in {'text/html', 'application/octet-stream', 'text/plain'}:
            raise DownloadError('unexpected_attachment_type')
        chunks, size = [], 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > MAX_BYTES:
                raise DownloadError('source_too_large')
            chunks.append(chunk)
        try:
            return b''.join(chunks).decode('utf-8')
        except UnicodeDecodeError:
            raise DownloadError('invalid_html_encoding') from None
    finally:
        response.close()


def implementation_hash() -> str:
    digest = hashlib.sha256()
    root = Path(__file__).parent
    for name in ['newsletter_download.py','newsletter_inline.py','newsletter_job.py','newsletter_activate.py']:
        digest.update(name.encode())
        digest.update((root / name).read_bytes())
    return digest.hexdigest()


def validate_proof(proof: dict, project: str):
    if (not isinstance(proof, dict) or proof.get('grant') != GRANT or proof.get('project_id') != project
            or proof.get('implementation_hash') != implementation_hash()
            or proof.get('live_attachment_read_and_parsed') is not True
            or not isinstance(proof.get('source_hash'), str)
            or not re.fullmatch('[a-f0-9]{64}', proof['source_hash'])
            or not isinstance(proof.get('task_id'), str) or not proof['task_id'].isalnum()):
        raise DownloadError('live_attachment_proof_required')
