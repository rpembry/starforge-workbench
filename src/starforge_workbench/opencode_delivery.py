"""One-attempt OpenCode delivery boundary for a future local instruction worker.

This module does not poll Workbench or control a provider session by itself. A
trusted local worker must supply an exact registered session ID and a claim.
"""

from dataclasses import dataclass, field
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import unicodedata
from urllib.parse import urlsplit


IDENTITY = re.compile(r'[A-Za-z0-9_-]{16,128}')
MODEL_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/+-]{0,127}')


class DeliveryError(ValueError):
    """Invalid local configuration or unsafe instruction envelope."""


@dataclass(frozen=True)
class DeliveryResult:
    state: str
    reason: str
    message_id: str


@dataclass(frozen=True)
class ResponseEvidence:
    instruction_id: str
    lease_token: str = field(repr=False)
    outcome: str
    reason: str


def _private_root(path):
    root = Path(path).absolute()
    if root.resolve() != root or not root.is_dir():
        raise DeliveryError('Local delivery state must be an existing real directory')
    info = root.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise DeliveryError('Local delivery state must be caller-owned and private')
    return root


def _origin(value):
    if not isinstance(value, str):
        raise DeliveryError('OpenCode API must be an explicit loopback HTTP origin')
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise DeliveryError('Invalid bounded OpenCode API port') from exc
    if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or
        parsed.username or parsed.password or parsed.path or parsed.query or
        parsed.fragment or port is None or port < 1):
        raise DeliveryError('OpenCode API must be an explicit loopback HTTP origin')
    return port


def _save(path, receipt, exclusive=False):
    data = (json.dumps(receipt, sort_keys=True) + '\n').encode()
    fd, temporary = tempfile.mkstemp(prefix='.delivery-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            # Publishing a complete inode with link is exclusive and atomic.
            os.link(temporary, path, follow_symlinks=False)
        else:
            os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _load(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
            info.st_mode & 0o077 or info.st_size > 4096):
            raise DeliveryError('Unsafe local delivery receipt')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            receipt = json.loads(stream.read(4097))
            if not isinstance(receipt, dict):
                raise DeliveryError('Invalid local delivery receipt')
            return receipt
    except (ValueError, UnicodeError) as exc:
        raise DeliveryError('Invalid local delivery receipt') from exc
    finally:
        os.close(fd)


class OpenCodeDelivery:
    """A single logical instruction is never posted a second time.

    The state root must be persistent and exclusive to this local worker. It
    contains opaque IDs and phases, never instruction text or provider output.
    """

    def __init__(self, origin, state_root, provider_id, model_id, timeout=5, transport=None):
        self.port = _origin(origin)
        self.root = _private_root(state_root)
        if (not isinstance(provider_id, str) or not isinstance(model_id, str) or
            not MODEL_ID.fullmatch(provider_id) or not MODEL_ID.fullmatch(model_id)):
            raise DeliveryError('Model identity must come from validated local configuration')
        if type(timeout) not in (int, float) or not 0 < timeout <= 30:
            raise DeliveryError('Invalid bounded OpenCode timeout')
        self.model = {'providerID': provider_id, 'modelID': model_id}
        self.timeout = timeout
        self.transport = transport or self._http

    def _http(self, method, path, payload=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=self.timeout)
        try:
            body = None if payload is None else json.dumps(payload).encode()
            headers = {} if body is None else {'Content-Type': 'application/json'}
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            if method == 'GET' and path.endswith('/message?limit=100'):
                # The API includes message parts. They remain local, are never
                # logged or persisted here, and are bounded before parsing.
                data = response.read(8 * 1024 * 1024 + 1)
                if len(data) > 8 * 1024 * 1024:
                    return 413, b''
            # Only the exact-session preflight needs another response body.
            elif method == 'GET' and path.count('/') == 2:
                data = response.read(8193)
            else:
                data = b''
            return response.status, data
        finally:
            connection.close()

    def _lookup(self, session_id, message_id):
        try:
            status, _ = self.transport('GET', f'/session/{session_id}/message/{message_id}')
        except (OSError, TimeoutError, http.client.HTTPException):
            return 'unavailable'
        return 'present' if status == 200 else 'absent' if status == 404 else 'unavailable'

    def deliver(self, instruction_id, session_id, text, lease_token=None):
        if not isinstance(instruction_id, str) or not IDENTITY.fullmatch(instruction_id):
            raise DeliveryError('Invalid instruction identity')
        if not isinstance(session_id, str) or not IDENTITY.fullmatch(session_id) or not session_id.startswith('ses_'):
            raise DeliveryError('Invalid exact OpenCode session identity')
        if not isinstance(text, str):
            raise DeliveryError('Instruction must be text')
        if lease_token is not None and (not isinstance(lease_token, str) or
                                        not IDENTITY.fullmatch(lease_token)):
            raise DeliveryError('Invalid response-reporting lease')
        text = text.strip()
        if not 1 <= len(text) <= 2000 or any(
            unicodedata.category(char) == 'Cc' and char not in '\n\t' for char in text
        ):
            raise DeliveryError('Instruction is empty, oversized, or contains control characters')
        content_hash = hashlib.sha256(text.encode()).hexdigest()
        message_id = 'msg_' + hashlib.sha256(instruction_id.encode()).hexdigest()[:32]
        path = self.root / (hashlib.sha256(instruction_id.encode()).hexdigest() + '.json')
        if path.is_symlink():
            raise DeliveryError('Unsafe local delivery receipt')
        if path.exists():
            receipt = _load(path)
            if (receipt.get('session_id') != session_id or receipt.get('message_id') != message_id or
                receipt.get('content_sha256') != content_hash):
                raise DeliveryError('Instruction identity changed target')
            if receipt.get('state') == 'received':
                return DeliveryResult('received', 'prior_admission', message_id)
            # An attempted POST may have crossed the provider boundary. A 404
            # here is not a negative acknowledgement and cannot justify replay.
            found = self._lookup(session_id, message_id)
            if found == 'present':
                receipt['state'] = 'received'
                _save(path, receipt)
                return DeliveryResult('received', 'message_record_found', message_id)
            receipt['state'] = 'uncertain'
            _save(path, receipt)
            return DeliveryResult('uncertain', 'prior_attempt_not_proven', message_id)

        try:
            status, body = self.transport('GET', f'/session/{session_id}')
        except (OSError, TimeoutError, http.client.HTTPException):
            return DeliveryResult('unavailable', 'session_preflight_unavailable', message_id)
        if status == 404:
            return DeliveryResult('vanished', 'session_not_found', message_id)
        if status != 200 or len(body) > 8192:
            return DeliveryResult('unavailable', 'session_preflight_unavailable', message_id)
        try:
            if json.loads(body).get('id') != session_id:
                return DeliveryResult('unavailable', 'session_identity_mismatch', message_id)
        except (ValueError, AttributeError):
            return DeliveryResult('unavailable', 'session_preflight_invalid', message_id)
        found = self._lookup(session_id, message_id)
        if found != 'absent':
            return DeliveryResult('uncertain' if found == 'present' else 'unavailable',
                                  'message_identity_in_use' if found == 'present' else 'message_preflight_unavailable',
                                  message_id)
        receipt = {'session_id': session_id, 'message_id': message_id,
                   'content_sha256': content_hash, 'state': 'attempted'}
        if lease_token is not None:
            receipt.update(instruction_id=instruction_id, lease_token=lease_token,
                           response_state='pending')
        try:
            _save(path, receipt, exclusive=True)
        except FileExistsError:
            return DeliveryResult('uncertain', 'concurrent_local_attempt', message_id)
        payload = {'messageID': message_id, 'model': self.model,
                   'parts': [{'type': 'text', 'text': text}]}
        try:
            status, _ = self.transport('POST', f'/session/{session_id}/prompt_async', payload)
        except (OSError, TimeoutError, http.client.HTTPException):
            status = None
        if status == 204:
            receipt['state'] = 'received'
            _save(path, receipt)
            return DeliveryResult('received', 'api_admitted', message_id)
        receipt['state'] = 'uncertain'
        _save(path, receipt)
        return DeliveryResult('uncertain', 'ambiguous_provider_boundary', message_id)

    def pending_responses(self):
        """Return terminal response evidence without provider content."""
        evidence = []
        for path in sorted(self.root.glob('*.json')):
            if not re.fullmatch(r'[a-f0-9]{64}\.json', path.name):
                continue
            receipt = _load(path)
            if receipt.get('state') != 'received' or receipt.get('response_state') != 'pending':
                continue
            instruction_id = receipt.get('instruction_id')
            lease_token = receipt.get('lease_token')
            session_id = receipt.get('session_id')
            message_id = receipt.get('message_id')
            if (not isinstance(instruction_id, str) or not IDENTITY.fullmatch(instruction_id) or
                path.name != hashlib.sha256(instruction_id.encode()).hexdigest() + '.json' or
                not isinstance(lease_token, str) or not IDENTITY.fullmatch(lease_token) or
                not isinstance(session_id, str) or not IDENTITY.fullmatch(session_id) or
                not isinstance(message_id, str) or not IDENTITY.fullmatch(message_id)):
                raise DeliveryError('Invalid local response receipt')
            result = self._response(session_id, message_id)
            if result:
                outcome, reason = result
                evidence.append(ResponseEvidence(instruction_id, lease_token, outcome, reason))
        return evidence

    def _response(self, session_id, message_id):
        try:
            status, body = self.transport(
                'GET', f'/session/{session_id}/message?limit=100')
        except (OSError, TimeoutError, http.client.HTTPException):
            return None
        if status != 200 or len(body) > 8 * 1024 * 1024:
            return None
        try:
            messages = json.loads(body)
            if not isinstance(messages, list):
                return None
            matched = []
            for item in messages:
                info = item.get('info') if isinstance(item, dict) else None
                if (isinstance(info, dict) and info.get('role') == 'assistant' and
                        info.get('parentID') == message_id):
                    matched.append(info)
        except (UnicodeError, ValueError, AttributeError):
            return None
        for info in matched:
            if isinstance(info.get('error'), dict):
                return 'responded', 'provider_response_error'
        for info in matched:
            completed = info.get('time', {}).get('completed') if isinstance(info.get('time'), dict) else None
            if (not isinstance(completed, bool) and isinstance(completed, (int, float)) and
                    info.get('finish') == 'stop'):
                return 'responded', 'provider_response_without_error'
        return None

    def mark_response(self, instruction_id, state):
        if state not in {'reported', 'unreportable'}:
            raise DeliveryError('Invalid response receipt state')
        path = self.root / (hashlib.sha256(instruction_id.encode()).hexdigest() + '.json')
        receipt = _load(path)
        if receipt.get('instruction_id') != instruction_id:
            raise DeliveryError('Instruction identity changed target')
        receipt['response_state'] = state
        receipt.pop('lease_token', None)
        _save(path, receipt)
