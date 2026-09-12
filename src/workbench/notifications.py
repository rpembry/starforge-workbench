"""Opt-in, read-only attention notifications with private restart-safe state."""
import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import tempfile
import time
import uuid
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .client import client

ENDPOINT = 'https://api.pushover.net/1/messages.json'
CATEGORIES = {'approval_needed', 'collector_health', 'agent_without_active_run',
              'provider_permission_wait', 'provider_user_question', 'provider_error'}
PROVIDER_CATEGORIES = {'provider_permission_wait', 'provider_user_question', 'provider_error'}
LABELS = {'approval_needed': 'approval request(s)', 'collector_health': 'collector visibility alert(s)',
          'agent_without_active_run': 'agent action(s) with unknown progress',
          'provider_permission_wait': 'provider permission request(s)',
          'provider_user_question': 'provider question(s)',
          'provider_error': 'provider error alert(s)'}


class NotificationError(Exception):
    def __init__(self, code, retryable=False, unknown=False):
        super().__init__(code)
        self.code, self.retryable, self.unknown = code, retryable, unknown


class QuietHours(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    timezone: str = Field(min_length=1, max_length=100)
    start: str = Field(pattern=r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')
    end: str = Field(pattern=r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')

    @field_validator('timezone')
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError('Use an installed IANA timezone name') from None
        return value

    @model_validator(mode='after')
    def nonempty_window(self):
        if self.start == self.end:
            raise ValueError('Quiet-hours start and end must differ')
        return self


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    enabled: bool = False
    dashboard_url: str | None = None
    api_client_file: str | None = None
    credentials_file: str | None = None
    state_dir: str = '~/.local/state/starforge-workbench-notifier'
    title: str = Field(default='Workbench', min_length=1, max_length=250)
    categories: list[Literal['approval_needed', 'collector_health', 'agent_without_active_run',
                             'provider_permission_wait', 'provider_user_question', 'provider_error']] = Field(default_factory=lambda: sorted(CATEGORIES))
    include_details: bool = False
    poll_seconds: int = Field(default=30, ge=10, le=3600)
    min_interval_seconds: int = Field(default=300, ge=5, le=86400)
    max_attempts: int = Field(default=3, ge=1, le=5)
    quiet_hours: QuietHours | None = None

    @field_validator('dashboard_url')
    @classmethod
    def dashboard(cls, value):
        if value is not None:
            parsed = urlsplit(value)
            if (len(value) > 512 or parsed.scheme != 'https' or not parsed.hostname or
                    parsed.username or parsed.password or parsed.query or parsed.fragment or
                    any(ord(ch) < 33 for ch in value)):
                raise ValueError('Use a credential-free HTTPS dashboard URL without query or fragment')
        return value


class LegacyEntry(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    fingerprint: str = Field(pattern=r'^[a-f0-9]{64}$')
    delivered: bool = False


class LegacyState(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    version: Literal[1] = 1
    scope: str
    entries: dict[str, LegacyEntry] = Field(default_factory=dict)
    attempts: int = Field(default=0, ge=0, le=5)
    not_before: float = Field(default=0, ge=0, allow_inf_nan=False)
    last_snapshot: float = Field(default=0, ge=0, allow_inf_nan=False)
    delivered_at: float | None = None
    blocked: bool = False


class Entry(LegacyEntry):
    occurrence: str = Field(default_factory=lambda: uuid.uuid4().hex)
    failures: int = Field(default=0, ge=0, le=5)
    blocked: bool = False
    outcome: Literal['pending', 'delivered', 'known_failed'] = 'pending'
    authority: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    authority_started_at: float | None = Field(default=None, allow_inf_nan=False)
    authority_sequence: int | None = Field(default=None, ge=0)
    incident: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')  # Optional only for pre-incident v2 state.

    @model_validator(mode='after')
    def complete_authority(self):
        fields = (self.authority, self.authority_started_at, self.authority_sequence)
        if any(value is not None for value in fields) and not all(value is not None for value in fields):
            raise ValueError('Authority metadata must be complete')
        if self.incident is not None and self.authority is None:
            raise ValueError('Incident metadata requires authority')
        return self


class Attempt(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    outcome: Literal['sending', 'unknown', 'delivered', 'known_failed', 'resolved_retry', 'resolved_delivered']
    members: dict[str, Entry]


class DeliveryState(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    version: Literal[2] = 2
    scope: str
    entries: dict[str, Entry] = Field(default_factory=dict)
    not_before: float = Field(default=0, ge=0, allow_inf_nan=False)
    last_snapshot: float = Field(default=0, ge=0, allow_inf_nan=False)
    delivered_at: float | None = None
    attempt: Attempt | None = None

    @property
    def blocked(self):
        return bool(self.attempt and self.attempt.outcome in {'sending', 'unknown'})


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def protected_read(path):
    """Never source credential files as shell code or echo their contents on error."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise NotificationError('unsafe_private_file')
        return stream.read()


def settings(path):
    try:
        return Settings.model_validate_json(protected_read(path))
    except FileNotFoundError:
        return Settings()  # No configuration never enables delivery.
    except (OSError, ValueError):
        raise NotificationError('invalid_notification_config') from None


def pushover_credentials(config):
    if not config.credentials_file:
        raise NotificationError('missing_pushover_credentials')
    try:
        values = {}
        for line in protected_read(Path(config.credentials_file).expanduser()).splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('export '):
                line = line[7:].lstrip()
            if '=' not in line:
                raise ValueError()
            key, value = line.split('=', 1)
            key = key.strip()
            if key not in {'PUSHOVER_USER_KEY', 'PUSHOVER_API_TOKEN', 'PUSHOVER_TITLE'} or key in values:
                raise ValueError()
            # dotenv-style unquoted titles may contain spaces; no expansion or execution.
            values[key] = ' '.join(shlex.split(value, comments=True))
        if any(not re.fullmatch(r'[A-Za-z0-9]{30}', values.get(key, ''))
               for key in ('PUSHOVER_USER_KEY', 'PUSHOVER_API_TOKEN')):
            raise ValueError()
        title = values.get('PUSHOVER_TITLE') or config.title
        if len(title) > 250 or any(ord(ch) < 32 for ch in title):
            raise ValueError()
        return values['PUSHOVER_USER_KEY'], values['PUSHOVER_API_TOKEN'], title
    except (OSError, ValueError):
        raise NotificationError('invalid_pushover_credentials') from None


def send_pushover(config, payload, transport=None):
    user, token, title = pushover_credentials(config)
    try:
        # A separate client never forwards Workbench credentials to Pushover.
        with httpx.Client(timeout=15, follow_redirects=False, transport=transport) as delivery:
            response = delivery.post(ENDPOINT, data={**payload, 'user': user, 'token': token, 'title': title})
        if 400 <= response.status_code < 500:
            raise NotificationError('delivery_rejected')
        if response.status_code != 200:
            raise NotificationError('delivery_unknown', unknown=True)
        result = response.json()
        if not isinstance(result, dict) or type(result.get('status')) is not int:
            raise NotificationError('delivery_unknown', unknown=True)
        if result['status'] != 1:
            raise NotificationError('delivery_rejected')
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
        raise NotificationError('delivery_not_submitted', retryable=True) from None
    except (httpx.HTTPError, ValueError):
        raise NotificationError('delivery_unknown', unknown=True) from None


def selected(snapshot, config, current):
    try:
        stamp = datetime.fromisoformat(snapshot['generated_at'])
        if stamp.tzinfo is None or not -30 <= current-stamp.timestamp() <= 180:
            raise ValueError()
        if not isinstance(snapshot['items'], list):
            raise ValueError()
        result = {}
        for item in snapshot['items']:
            if not isinstance(item, dict) or not isinstance(item.get('kind'), str):
                raise ValueError()
            if item['kind'] not in CATEGORIES:
                continue
            if any(not isinstance(item.get(k), str) or not item[k] for k in ('id', 'progress', 'reason', 'title')):
                raise ValueError()
            if item['kind'] in PROVIDER_CATEGORIES:
                provider_authority(item, stamp.timestamp())
            if item['kind'] not in config.categories:
                continue
            key = digest(item['id'])
            if key in result:
                if result[key] != item:
                    raise ValueError()
                continue
            result[key] = item
        return stamp.timestamp(), result
    except (ValueError, KeyError, TypeError, OverflowError):
        raise NotificationError('invalid_or_stale_attention_snapshot') from None


def fingerprint(item, include_details):
    # Evidence timestamps, polling order and heartbeat age never rearm alerts.
    keys = ('kind',) if item['kind'] in PROVIDER_CATEGORIES else ('kind', 'progress', 'reason')
    values = {key: item[key] for key in keys}
    if include_details:
        values['title'] = item['title']
    return digest(values)


def provider_authority(item, snapshot_stamp=None):
    if item['kind'] not in PROVIDER_CATEGORIES:
        return None
    evidence = item.get('evidence')
    if not isinstance(evidence, list) or len(evidence) != 1 or not isinstance(evidence[0], dict):
        raise ValueError()
    record = evidence[0]
    if record.get('resource') != 'provider_attention' or not isinstance(record.get('generation_id'), str) or not record['generation_id']:
        raise ValueError()
    if not isinstance(record.get('incident_id'), str) or not re.fullmatch(r'[a-f0-9]{64}', record['incident_id']):
        raise ValueError()
    if type(record.get('sequence')) is not int or record['sequence'] < 0:
        raise ValueError()
    timestamps = record.get('timestamps')
    if not isinstance(timestamps, dict):
        raise ValueError()
    started = datetime.fromisoformat(record.get('generation_started_at', ''))
    observed = datetime.fromisoformat(timestamps.get('observed_at', ''))
    fresh_until = datetime.fromisoformat(timestamps.get('fresh_until', ''))
    if any(value.tzinfo is None for value in (started, observed, fresh_until)):
        raise ValueError()
    started_stamp, observed_stamp, fresh_stamp = (value.timestamp() for value in (started, observed, fresh_until))
    if started_stamp > observed_stamp or observed_stamp >= fresh_stamp:
        raise ValueError()
    if snapshot_stamp is not None and observed_stamp > snapshot_stamp:
        raise ValueError()
    return digest(record['generation_id']), started_stamp, record['sequence'], record['incident_id']


def message(items, config):
    if not config.dashboard_url:
        raise NotificationError('dashboard_url_required')
    counts = Counter(item['kind'] for item in items)
    lines = ['Workbench needs attention: '+', '.join(f'{counts[k]} {LABELS[k]}' for k in sorted(counts))+'.',
             'Review the dashboard. Missing visibility or progress is not confirmed task failure.']
    if config.include_details:
        lines.extend('- '+item['title'][:160].replace('\n', ' ') for item in items[:4])
    return dict(message='\n'.join(lines)[:1024], url=config.dashboard_url, url_title='Open Workbench', priority='0')


def quiet_hours(config, current):
    quiet = config.quiet_hours
    if quiet is None:
        return None
    zone = ZoneInfo(quiet.timezone)
    local = datetime.fromtimestamp(current, zone)
    minute = local.strftime('%H:%M')
    active = ((quiet.start < quiet.end and quiet.start <= minute < quiet.end)
              or (quiet.start > quiet.end and (minute >= quiet.start or minute < quiet.end)))
    if not active:
        return None
    # Search UTC minute boundaries so nonexistent and repeated local times follow
    # the actual zone transition rather than an invented wall-clock instant.
    boundary = int(current//60)*60+60
    # A transition can skip the entire non-quiet interval, so the next
    # opening may be on the following day (including date-line jumps).
    for _ in range(72*60):
        candidate = datetime.fromtimestamp(boundary, zone).strftime('%H:%M')
        still_active = ((quiet.start < quiet.end and quiet.start <= candidate < quiet.end)
                        or (quiet.start > quiet.end and (candidate >= quiet.start or candidate < quiet.end)))
        if not still_active:
            return dict(timezone=quiet.timezone, start=quiet.start, end=quiet.end,
                        until=boundary, until_local=datetime.fromtimestamp(boundary, zone).isoformat())
        boundary += 60
    raise NotificationError('invalid_quiet_hours_window')


def scope(config):
    return digest([config.api_client_file, config.dashboard_url])


def read_state(folder, config):
    try:
        data = json.loads(protected_read(folder/'delivery.json'))
        if data.get('version') == 1:
            old = LegacyState.model_validate(data)
            migration_id = digest(data)
            entries = {key: Entry(**entry.model_dump(), occurrence=digest([migration_id, key]), outcome='delivered' if entry.delivered else 'pending',
                                  blocked=old.blocked, failures=old.attempts if not entry.delivered else 0)
                       for key, entry in old.entries.items()}
            pending = {key: entry.model_copy(deep=True) for key, entry in entries.items() if not entry.delivered}
            # V1 cannot prove whether an unacknowledged attempt reached the service.
            attempt = Attempt(id=migration_id, outcome='unknown', members=pending) if old.attempts and pending else None
            state = DeliveryState(scope=old.scope, entries=entries, not_before=old.not_before,
                                  last_snapshot=old.last_snapshot, delivered_at=old.delivered_at, attempt=attempt)
        else:
            state = DeliveryState.model_validate(data)
        if state.attempt and state.attempt.outcome == 'sending':
            state.attempt.outcome = 'unknown'
        if state.scope != scope(config):
            raise NotificationError('notification_state_source_mismatch')
        if any(not re.fullmatch(r'[a-f0-9]{64}', key) for key in state.entries):
            raise ValueError()
        return state
    except FileNotFoundError:
        return DeliveryState(scope=scope(config))
    except (OSError, ValueError):
        raise NotificationError('invalid_notification_state') from None


@contextmanager
def locked_state(folder):
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = folder.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise NotificationError('unsafe_notification_state_directory')
    fd = os.open(folder/'worker.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise NotificationError('unsafe_notification_lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise NotificationError('notifier_already_running') from None
        yield
    finally:
        os.close(fd)


def save_state(folder, state):
    fd, name = tempfile.mkstemp(prefix='.delivery-', dir=folder)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(state.model_dump_json())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, folder/'delivery.json')
        directory = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def reconcile(state, items, config, stamp):
    if stamp < state.last_snapshot:
        raise NotificationError('attention_snapshot_out_of_order')
    entries = {}
    for key, item in items.items():
        signature = fingerprint(item, config.include_details)
        prior = state.entries.get(key)
        authority = provider_authority(item, stamp)
        if authority and prior and prior.authority and prior.incident is not None:
            authority_id, started_at, sequence, incident = authority
            if started_at < prior.authority_started_at or (started_at == prior.authority_started_at and authority_id != prior.authority):
                raise NotificationError('provider_attention_generation_out_of_order')
            if authority_id == prior.authority:
                if started_at != prior.authority_started_at or incident != prior.incident or sequence < prior.authority_sequence:
                    raise NotificationError('provider_attention_evidence_out_of_order')
                if sequence == prior.authority_sequence and prior.fingerprint != signature:
                    raise NotificationError('provider_attention_evidence_conflict')
        if prior and prior.fingerprint == signature and (not authority or
                (authority[0] == prior.authority and authority[3] == prior.incident)):
            entries[key] = prior
            if authority:
                entries[key].authority_sequence = authority[2]
        else:
            fields = dict(fingerprint=signature)
            if authority:
                fields.update(authority=authority[0], authority_started_at=authority[1],
                              authority_sequence=authority[2], incident=authority[3])
            entries[key] = Entry(**fields)
    state.entries = entries  # Observed recovery rearms a later episode; API errors do not.
    state.last_snapshot = stamp
    return [item for key, item in items.items() if not entries[key].delivered]


def attempt_view(state):
    if not state.blocked:
        return None
    return dict(id=state.attempt.id, outcome='unknown', affected=len(state.attempt.members),
                blocks='whole_worker', warning='Delivery may already have occurred. Explicit retry can duplicate it.')


def resolve(config, attempt_id, outcome, acknowledge_duplicate=False):
    if outcome not in {'delivered', 'retry'}:
        raise NotificationError('invalid_resolution_outcome')
    if outcome == 'retry' and not acknowledge_duplicate:
        raise NotificationError('possible_duplicate_acknowledgement_required')
    folder = Path(config.state_dir).expanduser()
    with locked_state(folder):
        state = read_state(folder, config)
        if not state.blocked or state.attempt.id != attempt_id:
            raise NotificationError('unknown_attempt_mismatch')
        for key, member in state.attempt.members.items():
            entry = state.entries.get(key)
            if entry and (entry.fingerprint, entry.occurrence) == (member.fingerprint, member.occurrence):
                entry.delivered = outcome == 'delivered'
                entry.outcome = 'delivered' if entry.delivered else 'pending'
                entry.failures, entry.blocked = 0, False
        state.attempt.outcome = 'resolved_'+outcome
        save_state(folder, state)
        return {'status': state.attempt.outcome, 'attempt_id': attempt_id}


def poll(config, snapshot, sender=None, current=None, preview=False, retry_pending=False):
    if not config.enabled and not preview:
        return {'status': 'disabled'}
    current = time.time() if current is None else current
    stamp, items = selected(snapshot, config, current)
    folder = Path(config.state_dir).expanduser()
    if preview:
        state = read_state(folder, config)
        pending = reconcile(state, items, config, stamp)
        eligible = sum(not entry.delivered and not entry.blocked and entry.failures < config.max_attempts
                       for entry in state.entries.values())
        deferred = quiet_hours(config, current) if eligible and not state.blocked else None
        if state.blocked:
            delivery = 'blocked_unknown'
        elif not pending:
            delivery = 'none'
        elif not eligible:
            delivery = 'blocked'
        elif deferred:
            delivery = 'deferred_quiet_hours'
        else:
            delivery = 'backoff' if current < state.not_before else 'eligible'
        return dict(status='preview', pending=len(pending), blocked=state.blocked,
                    attempt=attempt_view(state), blocked_items=sum(e.blocked for e in state.entries.values() if not e.delivered), not_before=state.not_before,
                    quiet_hours=deferred, delivery=delivery,
                    payload=message(pending, config) if pending else None)
    with locked_state(folder):
        state = read_state(folder, config)
        reconcile(state, items, config, stamp)
        if state.blocked:
            save_state(folder, state)
            return dict(status='unknown', attempt=attempt_view(state))
        if retry_pending:
            for entry in state.entries.values():
                if not entry.delivered:
                    entry.blocked, entry.failures = False, 0
        pending = {key: item for key, item in items.items() if not state.entries[key].delivered}
        eligible = {key: item for key, item in pending.items()
                    if not state.entries[key].blocked and state.entries[key].failures < config.max_attempts}
        if not eligible:
            save_state(folder, state)
            return {'status': 'blocked' if pending else 'quiet', 'pending': len(pending)}
        deferred = quiet_hours(config, current)
        if deferred:
            save_state(folder, state)
            return {'status': 'deferred', 'reason': 'quiet_hours', 'pending': len(pending),
                    'eligible': len(eligible), 'quiet_hours': deferred}
        if current < state.not_before:
            save_state(folder, state)
            return {'status': 'backoff', 'pending': len(pending), 'not_before': state.not_before}
        payload = message(list(eligible.values()), config)
        state.attempt = Attempt(outcome='sending', members={key: state.entries[key].model_copy(deep=True) for key in eligible})
        failures = max(state.entries[key].failures for key in eligible)
        state.not_before = current+max(config.min_interval_seconds, 30*2**failures)
        save_state(folder, state)  # A crash from here through acknowledgement persistence is unknown.
        try:
            (sender or send_pushover)(config, payload)
        except NotificationError as exc:
            state.attempt.outcome = 'unknown' if exc.unknown else 'known_failed'
            if not exc.unknown:
                for key in eligible:
                    entry = state.entries[key]
                    entry.failures += 1
                    entry.outcome = 'known_failed'
                    entry.blocked = not exc.retryable or entry.failures >= config.max_attempts
            save_state(folder, state)
            return dict(status='unknown' if exc.unknown else ('blocked' if all(state.entries[k].blocked for k in eligible) else 'retry_pending'),
                        pending=len(pending), error=exc.code, attempt=attempt_view(state))
        for key in eligible:
            entry = state.entries[key]
            entry.delivered, entry.outcome = True, 'delivered'
            entry.failures, entry.blocked = 0, False
        state.attempt.outcome = 'delivered'
        state.delivered_at = current
        state.not_before = current+config.min_interval_seconds
        save_state(folder, state)
        return {'status': 'delivered', 'count': len(eligible)}


def fetch_snapshot(config):
    if not config.api_client_file:
        raise NotificationError('api_client_file_required')
    try:
        with client(credentials_file=Path(config.api_client_file).expanduser()) as api:
            response = api.get('/api/attention')
            response.raise_for_status()
            return response.json()
    except (httpx.HTTPError, OSError, ValueError, KeyError):
        raise NotificationError('attention_api_unavailable') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['once', 'watch', 'preview', 'test', 'status', 'resolve'])
    parser.add_argument('--config', type=Path, default=Path.home()/'.config/starforge-workbench/notifications.json')
    parser.add_argument('--snapshot', type=Path, help='Synthetic JSON snapshot for preview only')
    parser.add_argument('--send', action='store_true', help='Explicitly send one test notification (test command only)')
    parser.add_argument('--retry-pending', action='store_true', help='Explicitly unblock corrected delivery settings (once only)')
    parser.add_argument('--attempt-id')
    parser.add_argument('--outcome', choices=['delivered', 'retry'])
    parser.add_argument('--acknowledge-possible-duplicate', action='store_true')
    args = parser.parse_args(argv)
    if args.command == 'resolve' and (not args.attempt_id or not args.outcome):
        parser.error('Resolution requires the exact attempt ID and outcome')
    if args.command != 'resolve' and (args.attempt_id or args.outcome or args.acknowledge_possible_duplicate):
        parser.error('Resolution flags require resolve')
    if args.snapshot and args.command != 'preview' or args.send and args.command != 'test' or args.retry_pending and args.command != 'once':
        parser.error('Snapshot, send, or retry flag used with the wrong command')
    while True:
        try:
            config = settings(args.config.expanduser())
            if args.command == 'status':
                state = read_state(Path(config.state_dir).expanduser(), config)
                print(json.dumps(dict(status='unknown' if state.blocked else 'idle', attempt=attempt_view(state),
                                      pending=sum(not e.delivered for e in state.entries.values()),
                                      known_failed=sum(e.outcome == 'known_failed' for e in state.entries.values()),
                                      blocked_items=sum(e.blocked for e in state.entries.values() if not e.delivered))))
                return 0
            if args.command == 'resolve':
                print(json.dumps(resolve(config, args.attempt_id, args.outcome, args.acknowledge_possible_duplicate)))
                return 0
            if args.command == 'test':
                payload = dict(message='Workbench notification delivery test. No task state was changed.',
                               url=config.dashboard_url, url_title='Open Workbench', priority='0')
                if not config.dashboard_url:
                    raise NotificationError('dashboard_url_required')
                if args.send:
                    send_pushover(config, payload)
                print(json.dumps({'status': 'test_delivered' if args.send else 'test_preview', 'payload': payload}))
                return 0
            if not config.enabled and args.command != 'preview':
                print(json.dumps({'status': 'disabled'}))
                return 0
            snapshot = json.loads(args.snapshot.read_text()) if args.snapshot else fetch_snapshot(config)
            result = poll(config, snapshot, preview=args.command == 'preview', retry_pending=args.retry_pending)
            print(json.dumps(result), flush=True)
        except (NotificationError, OSError, ValueError):
            # Never log response bodies, config input, keys, task data or HTTP exceptions.
            import sys
            exc = sys.exception()
            result = {'status': 'error', 'error': exc.code if isinstance(exc, NotificationError) else 'notification_local_error'}
            print(json.dumps(result), flush=True)
        if args.command != 'watch':
            return 2 if result['status'] in {'error', 'blocked', 'retry_pending', 'unknown'} else 0
        time.sleep(config.poll_seconds if 'config' in locals() else 30)


if __name__ == '__main__':
    raise SystemExit(main())
