"""Verified local upgrade events; delivery remains owned by wb-notify.

A protected local configuration trusts the updater running as the same OS user.
The event's claim is not remote attestation or permission to install software.
"""
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import time
import tempfile
from typing import Literal
import uuid

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SYNC_URL = 'https://api.todoist.com/api/v1/sync'
CHANGELOG = 'https://developers.openai.com/codex/changelog'
VERSION = r'^[0-9]+(?:\.[0-9]+){1,5}(?:[a-zA-Z0-9.+_-]{0,32})$'
IDENTIFIER = r'^[a-zA-Z0-9_-]{1,100}$'
MAX_EVENTS = 10000
MAX_SOURCE_BYTES = 2 * 1024 * 1024


class UpgradeSettings(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    enabled: bool = False
    source_id: str = Field(default='codex-updaters', pattern=IDENTIFIER)
    project_id: str | None = Field(default=None, pattern=IDENTIFIER)
    token_file: str | None = None

    @model_validator(mode='after')
    def destination(self):
        if self.enabled and not self.project_id:
            raise ValueError('Enabled upgrade notes require an explicit destination')
        return self


class UpgradeEvent(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)
    schema_version: Literal[1] = 1
    app_id: Literal['codex_cli', 'codex_desktop']
    before_version: str = Field(pattern=VERSION, max_length=80)
    installed_version: str = Field(pattern=VERSION, max_length=80)

    @model_validator(mode='after')
    def changed(self):
        if self.before_version == self.installed_version:
            raise ValueError('Only changed, verified installations are upgrade events')
        return self


class Notes(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)
    status: Literal['matched', 'not_found', 'lookup_failed']
    highlights: list[str] = Field(default_factory=list, max_length=3)
    anchor: str | None = Field(default=None, pattern=r'^[a-zA-Z0-9_-]{1,160}$')

    @field_validator('highlights')
    @classmethod
    def bounded_text(cls, value):
        if any(not text or len(text) > 250 or any(ord(c) < 32 for c in text) for text in value):
            raise ValueError('Invalid notes text')
        return value

    @model_validator(mode='after')
    def matched_only(self):
        if self.status != 'matched' and (self.highlights or self.anchor):
            raise ValueError('Unmatched notes cannot carry highlights')
        return self


class TaskArguments(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)
    project_id: str = Field(pattern=IDENTIFIER)
    content: str = Field(min_length=1, max_length=250)
    description: str = Field(min_length=1, max_length=2000)

    @field_validator('content', 'description')
    @classmethod
    def text_only(cls, value, info):
        if any(ord(c) < 32 and not (info.field_name == 'description' and c == '\n') for c in value):
            raise ValueError('Invalid task text')
        return value


class UpgradeReceipt(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    event: UpgradeEvent
    notes: Notes | None = None
    task_args: TaskArguments | None = None
    command_id: str = Field(default_factory=lambda: str(uuid.uuid4()), pattern=r'^[a-f0-9-]{36}$')
    temp_id: str = Field(default_factory=lambda: str(uuid.uuid4()), pattern=r'^[a-f0-9-]{36}$')
    outcome: Literal['pending', 'sending', 'unknown', 'known_failed', 'delivered'] = 'pending'
    failures: int = Field(default=0, ge=0, le=5)
    not_before: float = Field(default=0, ge=0, allow_inf_nan=False)
    task_id: str | None = Field(default=None, pattern=IDENTIFIER)

    @model_validator(mode='after')
    def complete(self):
        uuid.UUID(self.command_id)
        uuid.UUID(self.temp_id)
        if self.outcome in {'sending', 'unknown', 'delivered'} and (self.notes is None or self.task_args is None):
            raise ValueError('Dispatch requires immutable notes and task arguments')
        if (self.outcome == 'delivered') != (self.task_id is not None):
            raise ValueError('Delivery requires a task receipt')
        return self


class UpgradeState(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    version: Literal[1] = 1
    scope: str = Field(pattern=r'^[a-f0-9]{64}$')
    entries: dict[str, UpgradeReceipt] = Field(default_factory=dict, max_length=MAX_EVENTS)


class UpgradeEnvelope(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    scope: str = Field(pattern=r'^[a-f0-9]{64}$')
    event: UpgradeEvent


def policy(config):
    return config.upgrade_notes


def binding(config):
    from .notifications import digest
    p = policy(config)
    return digest(['upgrade_notes.v1', p.source_id, 'todoist', p.project_id])


def event_key(config, event):
    from .notifications import digest
    return digest([binding(config), event.app_id, event.installed_version])


def read_state(config):
    from .notifications import NotificationError, protected_read
    try:
        data = protected_read(Path(config.state_dir).expanduser() / 'upgrades.json')
        state = UpgradeState.model_validate_json(data)
        if state.scope != binding(config):
            raise NotificationError('upgrade_destination_mismatch')
        commands, temps = set(), set()
        for key, receipt in state.entries.items():
            if key != event_key(config, receipt.event) or receipt.command_id in commands or receipt.temp_id in temps:
                raise ValueError()
            if receipt.task_args and receipt.task_args.project_id != policy(config).project_id:
                raise ValueError()
            commands.add(receipt.command_id)
            temps.add(receipt.temp_id)
            if receipt.outcome == 'sending':
                receipt.outcome = 'unknown'
        return state
    except FileNotFoundError:
        return UpgradeState(scope=binding(config))
    except (OSError, ValueError):
        raise NotificationError('invalid_upgrade_state') from None


def save(config, state):
    from .notifications import save_state
    save_state(Path(config.state_dir).expanduser(), state, 'upgrades.json')


def record(config, event):
    """No network or credentials. Preserve the first event for each app/version."""
    from .notifications import NotificationError, private_state_directory, protected_read
    if not policy(config) or not policy(config).enabled:
        return {'status': 'disabled'}
    event = UpgradeEvent.model_validate(event)
    folder = Path(config.state_dir).expanduser()
    private_state_directory(folder)
    inbox = folder/'upgrade-inbox'
    private_state_directory(inbox)
    fsync_directory(folder.parent)
    fsync_directory(folder)
    state = read_state(config)  # Atomic writer makes this lock-free read safe.
    key = event_key(config, event)
    if key in state.entries:
        return {'status': 'already_recorded', 'event_id': key}
    pending = list(inbox.glob('*.json'))
    if len(state.entries) + len(pending) >= MAX_EVENTS:
        raise NotificationError('upgrade_receipt_limit')
    envelope = UpgradeEnvelope(scope=binding(config), event=event)
    fd, name = tempfile.mkstemp(prefix='.upgrade-', dir=inbox)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(envelope.model_dump_json())
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # Link, never replace: concurrent publication preserves first bytes.
            os.link(name, inbox/f'{key}.json', follow_symlinks=False)
        except FileExistsError:
            try:
                prior = UpgradeEnvelope.model_validate_json(protected_read(inbox/f'{key}.json'))
                if prior.scope != envelope.scope or event_key(config, prior.event) != key:
                    raise ValueError()
            except FileNotFoundError:
                # Import persisted a permanent receipt before removing its input.
                if key not in read_state(config).entries:
                    raise NotificationError('invalid_upgrade_inbox') from None
            except (OSError, ValueError):
                raise NotificationError('invalid_upgrade_inbox') from None
            fsync_directory(inbox)
            return {'status': 'already_recorded', 'event_id': key}
        fsync_directory(inbox)
        return {'status': 'recorded', 'event_id': key}
    finally:
        os.unlink(name)


def fsync_directory(folder):
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def import_inbox(config, state):
    """Caller holds worker lock. Persist before removing acknowledged inputs."""
    from .notifications import NotificationError, private_state_directory, protected_read
    inbox = Path(config.state_dir).expanduser()/'upgrade-inbox'
    private_state_directory(inbox)
    paths = list(inbox.glob('*.json'))
    if len(paths) > MAX_EVENTS:
        raise NotificationError('upgrade_receipt_limit')
    for path in paths:
        try:
            envelope = UpgradeEnvelope.model_validate_json(protected_read(path))
            key = event_key(config, envelope.event)
            if envelope.scope != binding(config) or path.name != f'{key}.json':
                raise ValueError()
        except (OSError, ValueError):
            raise NotificationError('invalid_upgrade_inbox') from None
        if key not in state.entries:
            if len(state.entries) >= MAX_EVENTS:
                raise NotificationError('upgrade_receipt_limit')
            state.entries[key] = UpgradeReceipt(event=envelope.event)
    if paths:
        save(config, state)
        for path in paths:
            path.unlink()
        fsync_directory(inbox)


def plain(text):
    # Remote notes are untrusted text, not Todoist Markdown instructions/links.
    text = re.sub(r'\[([^]]+)\]\([^)]+\)', r'\1', text)
    text = re.sub(r'https?://\S+', '', text)
    text = ''.join(c if ord(c) >= 32 else ' ' for c in text)
    text = re.sub(r'[`*_\[\]<>\\]', '', text)
    return ' '.join(text.split())[:250]


class AppChangelog(HTMLParser):
    """Capture only heading and highlight text in codex-app changelog items."""
    def __init__(self):
        super().__init__()
        self.entries = []
        self.depth = 0
        self.item = None
        self.capture = None
        self.text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if not self.depth and tag == 'li' and 'codex-app' in attrs.get('data-codex-topics', '').split(','):
            self.item = {'anchor': attrs.get('id'), 'heading': '', 'highlights': []}
            self.depth = 1
        elif self.depth and tag == 'li':
            self.depth += 1
        if self.depth and self.capture is None and tag in {'h2', 'h3', 'h4', 'p'}:
            self.capture, self.text = tag, []

    def handle_data(self, data):
        if self.capture:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag == self.capture:
            value = ' '.join(self.text)
            if tag.startswith('h'):
                self.item['heading'] += value
            elif value.strip():
                self.item['highlights'].append(plain(value))
            self.capture = None
        if self.depth and tag == 'li':
            self.depth -= 1
            if not self.depth:
                self.entries.append(self.item)
                self.item, self.capture = None, None


def lookup_notes(event, transport=None):
    """Fixed official sources, bounded bodies, no redirects or local credentials."""
    url = (f'https://api.github.com/repos/openai/codex/releases/tags/rust-v{event.installed_version}'
           if event.app_id == 'codex_cli' else CHANGELOG)
    try:
        with httpx.Client(timeout=15, follow_redirects=False, transport=transport, trust_env=False) as client:
            with client.stream('GET', url, headers={'User-Agent': 'starforge-upgrade-notes'}) as response:
                if response.status_code == 404 and event.app_id == 'codex_cli':
                    return Notes(status='not_found')
                if response.status_code != 200:
                    return Notes(status='lookup_failed')
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_SOURCE_BYTES:
                        return Notes(status='lookup_failed')
        if event.app_id == 'codex_cli':
            release = json.loads(body)
            if not isinstance(release, dict) or release.get('tag_name') != f'rust-v{event.installed_version}':
                return Notes(status='lookup_failed')
            text = release.get('body')
            if not isinstance(text, str):
                return Notes(status='lookup_failed')
            highlights = [plain(line[2:]) for line in text.splitlines()
                          if re.match(r'^[-*] ', line) and not line[2:].startswith('Full Changelog')]
            return Notes(status='matched', highlights=[h for h in highlights if h][:3])
        parser = AppChangelog()
        parser.feed(body.decode('utf-8'))
        for item in parser.entries:
            # Exact version token, never a release-train substring match.
            if re.search(r'(?<![\w.+-])' + re.escape(event.installed_version) + r'(?![\w.+-])', item['heading']):
                anchor = item['anchor']
                if anchor is not None and not re.fullmatch(r'[a-zA-Z0-9_-]{1,160}', anchor):
                    anchor = None
                return Notes(status='matched', highlights=[h for h in item['highlights'] if h][:3], anchor=anchor)
        return Notes(status='not_found')
    except (httpx.HTTPError, UnicodeError, ValueError, TypeError):
        return Notes(status='lookup_failed')


def payload(config, receipt):
    event, notes = receipt.event, receipt.notes
    label = 'Codex CLI' if event.app_id == 'codex_cli' else 'Codex desktop'
    url = (f'https://github.com/openai/codex/releases/tag/rust-v{event.installed_version}'
           if event.app_id == 'codex_cli' else CHANGELOG + (f'#{notes.anchor}' if notes.anchor else ''))
    status = {'matched': 'Official notes matched this exact version.',
              'not_found': 'No matching notes found in the checked official source.',
              'lookup_failed': 'Official notes lookup failed; matching notes are unverified.'}[notes.status]
    description = '\n'.join([f'Installed {event.before_version} → {event.installed_version}.', status,
                             *[f'- {h}' for h in notes.highlights], f'Release notes: {url}'])
    return {'project_id': policy(config).project_id,
            'content': f'{label} upgraded to {event.installed_version} — release notes', 'description': description}


def send_todoist(config, receipt, transport=None):
    """One stable Sync command UUID; no account-resource query or blind replay."""
    from .notifications import NotificationError, protected_read
    p = policy(config)
    if receipt.task_args is None or receipt.task_args.project_id != p.project_id:
        raise NotificationError('invalid_upgrade_dispatch')
    if not p.token_file:
        raise NotificationError('todoist_token_not_configured')
    try:
        token = protected_read(Path(p.token_file).expanduser()).strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]{20,256}', token):
            raise ValueError()
    except (OSError, ValueError, NotificationError):
        raise NotificationError('invalid_todoist_token') from None
    command = {'type': 'item_add', 'uuid': receipt.command_id, 'temp_id': receipt.temp_id,
               'args': receipt.task_args.model_dump()}
    try:
        with httpx.Client(timeout=15, follow_redirects=False, transport=transport, trust_env=False) as delivery:
            with delivery.stream('POST', SYNC_URL, headers={'Authorization': f'Bearer {token}'},
                                 data={'commands': json.dumps([command]), 'resource_types': '[]'}) as response:
                if response.status_code in {400, 401, 403, 404, 422, 429}:
                    raise NotificationError('todoist_rejected')
                if response.status_code != 200:
                    raise NotificationError('todoist_delivery_unknown', unknown=True)
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_SOURCE_BYTES:
                        raise NotificationError('todoist_delivery_unknown', unknown=True)
        data = json.loads(body)
        status = data.get('sync_status', {}).get(receipt.command_id)
        task_id = data.get('temp_id_mapping', {}).get(receipt.temp_id)
        if isinstance(status, dict) and type(status.get('error_code')) is int:
            raise NotificationError('todoist_rejected')
        if status != 'ok' or not isinstance(task_id, str) or not re.fullmatch(IDENTIFIER, task_id):
            raise NotificationError('todoist_delivery_unknown', unknown=True)
        return task_id
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
        raise NotificationError('todoist_not_submitted', retryable=True) from None
    except (httpx.HTTPError, ValueError, AttributeError, TypeError):
        raise NotificationError('todoist_delivery_unknown', unknown=True) from None


def poll(config, sender=None, lookup=None, current=None, preview=False):
    from .notifications import NotificationError, locked_state, quiet_hours
    if not policy(config) or not policy(config).enabled:
        return {'status': 'disabled'}
    now = time.time() if current is None else current
    folder = Path(config.state_dir).expanduser()
    # Preview cannot create folders, lock, read credentials, lookup or send.
    if preview:
        state = read_state(config)
        return {'status': 'preview', 'pending': sum(e.outcome != 'delivered' for e in state.entries.values()),
                'inbox_pending': len(list((folder/'upgrade-inbox').glob('*.json')))}
    with locked_state(folder):
        state = read_state(config)
        import_inbox(config, state)
        if any(e.outcome == 'unknown' for e in state.entries.values()):
            return {'status': 'unknown'}
        if quiet_hours(config, now):
            return {'status': 'quiet_hours'}
        for key, receipt in state.entries.items():
            if receipt.outcome != 'pending' or receipt.not_before > now:
                continue
            if receipt.notes is None:
                receipt.notes = (lookup or lookup_notes)(receipt.event)
                receipt.notes = Notes.model_validate(receipt.notes)
            if receipt.task_args is None:
                receipt.task_args = TaskArguments.model_validate(payload(config, receipt))
            receipt.outcome = 'sending'
            save(config, state)  # fsynced immutable bytes/UUID intent precedes HTTP.
            try:
                task_id = (sender or send_todoist)(config, receipt.model_copy(deep=True))
                if not isinstance(task_id, str) or not re.fullmatch(IDENTIFIER, task_id):
                    raise NotificationError('todoist_delivery_unknown', unknown=True)
            except NotificationError as exc:
                receipt.failures += 1
                receipt.outcome = ('unknown' if exc.unknown else
                                   'pending' if exc.retryable and receipt.failures < config.max_attempts else 'known_failed')
                receipt.not_before = now + config.min_interval_seconds
                save(config, state)
                return {'status': 'unknown' if exc.unknown else 'retry_pending' if receipt.outcome == 'pending' else 'blocked',
                        'event_id': key, 'error': exc.code}
            except Exception:
                receipt.outcome = 'unknown'
                save(config, state)
                return {'status': 'unknown', 'event_id': key, 'error': 'todoist_delivery_unknown'}
            receipt.task_id, receipt.outcome = task_id, 'delivered'
            save(config, state)  # failure here leaves sending on disk => unknown on restart.
            return {'status': 'delivered', 'event_id': key}
        return {'status': 'blocked' if any(e.outcome == 'known_failed' for e in state.entries.values()) else 'idle'}


def reconcile(config, event_id, command_id, task_id):
    """Operator confirms the exact existing task; never creates or replays a task."""
    from .notifications import NotificationError, locked_state
    if not policy(config) or not policy(config).enabled:
        return {'status': 'disabled'}
    with locked_state(Path(config.state_dir).expanduser()):
        state = read_state(config)
        receipt = state.entries.get(event_id)
        if not receipt or receipt.command_id != command_id or receipt.outcome not in {'unknown', 'known_failed'}:
            raise NotificationError('upgrade_reconciliation_mismatch')
        if not isinstance(task_id, str) or not re.fullmatch(IDENTIFIER, task_id):
            raise NotificationError('upgrade_reconciliation_mismatch')
        receipt.task_id, receipt.outcome = task_id, 'delivered'
        save(config, state)
        return {'status': 'resolved_delivered', 'event_id': event_id}


def status(config):
    if not policy(config) or not policy(config).enabled:
        return {'status': 'disabled'}
    state = read_state(config)
    return {'status': ('unknown' if any(e.outcome == 'unknown' for e in state.entries.values()) else
                       'blocked' if any(e.outcome == 'known_failed' for e in state.entries.values()) else 'idle'),
            'inbox_pending': len(list((Path(config.state_dir).expanduser()/'upgrade-inbox').glob('*.json'))),
            'entries': [{'event_id': key, 'command_id': e.command_id, 'outcome': e.outcome,
                         'task_id': e.task_id} for key, e in state.entries.items()]}


def retry_known_failure(config, event_id, command_id):
    """Explicit retry after correction, same UUID; uncertain dispatch is forbidden."""
    from .notifications import NotificationError, locked_state
    if not policy(config) or not policy(config).enabled:
        return {'status': 'disabled'}
    with locked_state(Path(config.state_dir).expanduser()):
        state = read_state(config)
        receipt = state.entries.get(event_id)
        if not receipt or receipt.command_id != command_id or receipt.outcome != 'known_failed':
            raise NotificationError('upgrade_retry_mismatch')
        receipt.outcome, receipt.failures, receipt.not_before = 'pending', 0, 0
        save(config, state)
        return {'status': 'retry_pending', 'event_id': event_id}
