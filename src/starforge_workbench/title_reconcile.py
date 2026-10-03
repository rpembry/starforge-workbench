"""Explicit, exact-ID Codex title reconciliation.

The installed Codex 0.155.1 app-server has thread/name/set, but no conditional
expected-title parameter.  Its read/set pair cannot guard another title writer.
The host adapter therefore deliberately refuses writes until Codex supplies an
atomic conditional rename interface.  Tests can inject a guarded adapter.
"""
from __future__ import annotations

import fcntl
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
THREAD_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,36}$")
MAX_TITLE = 200


def _now():
    return datetime.now(timezone.utc).isoformat()


def _private_dir(path: Path):
    if path.is_symlink():
        raise ValueError('Unsafe reconciliation state directory')
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError('Reconciliation state directory must be private')


def _write(path: Path, value):
    _private_dir(path.parent)
    temporary = path.with_name('.' + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read(path: Path):
    if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError('Unsafe reconciliation state file')
    return json.loads(path.read_text())


@contextmanager
def _lock(root: Path):
    _private_dir(root)
    fd = os.open(root / 'reconcile.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


class CodexCatalog:
    """Read-only catalog. Never writes provider files or bypasses its writer."""

    guarded_rename = False

    def __init__(self, home: Path | None = None):
        self.home = home or Path.home()

    def read(self, thread_id: str):
        if not THREAD_ID.fullmatch(thread_id):
            return None
        rows = []
        for path in (self.home / '.codex').glob('state*.sqlite'):
            uri = path.resolve().as_uri() + '?mode=ro'
            with sqlite3.connect(uri, uri=True, timeout=2) as db:
                row = db.execute('SELECT id, cwd, title, archived, source FROM threads WHERE id=?', (thread_id,)).fetchone()
                if row:
                    rows.append(row)
        if len(rows) != 1:
            return None
        identity, cwd, title, archived, source = rows[0]
        return {'id': identity, 'cwd': cwd, 'title': title, 'archived': bool(archived), 'source': source}

    def rename_if_title(self, thread_id: str, expected: str, desired: str):
        raise NotImplementedError('Codex 0.155.1 has no atomic expected-title rename')


class Reconciler:
    def __init__(self, contexts, bindings, catalog, state: Path, observed=None):
        self.contexts = contexts
        self.bindings = bindings
        self.catalog = catalog
        self.state = state
        self.observed = observed or (lambda context: None)

    def _snapshot(self, ids):
        by_id = {c['id']: c for c in self.contexts()}
        rows = []
        for context_id in ids:
            if not ID.fullmatch(context_id) or context_id not in by_id:
                raise ValueError('Select exact configured context IDs')
            context = by_id[context_id]
            desired = context.get('title', '')
            observed = self.observed(context)
            binding = self.bindings(context_id)
            thread_id = binding.get('id') if isinstance(binding, dict) else None
            reason = None
            if context.get('provider') != 'codex':
                reason = 'not_codex'
            elif not isinstance(desired, str) or not desired.strip():
                reason = 'blank_label'
            elif len(desired) > MAX_TITLE or any(ord(ch) < 32 or ord(ch) == 127 for ch in desired):
                reason = 'invalid_label'
            elif observed is not None and observed != desired:
                reason = 'label_disagreement'
            elif not thread_id or not THREAD_ID.fullmatch(str(thread_id)):
                reason = 'missing_binding'
            elif binding.get('provider') != 'codex' or binding.get('cwd') != context.get('cwd'):
                reason = 'stale_binding'
            record = None
            if not reason:
                try:
                    record = self.catalog.read(thread_id)
                except (OSError, sqlite3.Error, ValueError):
                    reason = 'catalog_unavailable'
                if not reason and (not record or record.get('archived')):
                    reason = 'missing_or_archived_thread'
                elif not reason and (record.get('id') != thread_id or record.get('cwd') != binding.get('source_cwd', binding['cwd']) or record.get('source') != 'cli'):
                    reason = 'stale_binding'
            rows.append({'context': context_id, 'label': desired, 'observed_label': observed,
                         'thread_id': thread_id, 'current_title': record.get('title') if record else None,
                         'desired_title': desired, 'binding': binding,
                         'mapping_freshness': 'unverified' if reason else 'fresh',
                         'status': 'conflict' if reason else
                         'already_matched' if record['title'] == desired else
                         'needs_choice' if record['title'] else 'eligible', 'reason': reason})
        aliases = {}
        # Alias conflicts matter even when only one of the contexts was selected.
        for context_id, context in by_id.items():
            if context.get('provider') != 'codex':
                continue
            binding = self.bindings(context_id)
            if isinstance(binding, dict) and binding.get('id'):
                aliases.setdefault(binding['id'], set()).add(context.get('title'))
        for row in rows:
            if row['thread_id'] and len(aliases[row['thread_id']]) > 1:
                row['status'], row['reason'] = 'conflict', 'ambiguous_alias'
                row['mapping_freshness'] = 'conflict'
        return rows

    @staticmethod
    def _public(plan):
        copy = deepcopy(plan)
        for row in copy['rows']:
            row.pop('binding', None)
        return copy

    def preview(self, ids):
        ids = list(ids)
        if not ids or len(ids) != len(set(ids)) or len(ids) > 50:
            raise ValueError('Select 1..50 distinct exact context IDs')
        rows = self._snapshot(ids)
        plan = {'plan_id': str(uuid.uuid4()), 'created_at': _now(), 'rows': rows, 'results': {}}
        with _lock(self.state):
            _write(self.state / (plan['plan_id'] + '.json'), plan)
        return self._public(plan)

    def _load_plan(self, plan_id):
        if not THREAD_ID.fullmatch(plan_id):
            raise ValueError('Invalid plan ID')
        return _read(self.state / (plan_id + '.json'))

    def status(self, plan_id):
        return self._public(self._load_plan(plan_id))

    def apply(self, plan_id, selected=None, all_eligible=False):
        if bool(selected) == bool(all_eligible):
            raise ValueError('Select exact context IDs or all eligible rows')
        with _lock(self.state):
            plan = self._load_plan(plan_id)
            originals = {row['context']: row for row in plan['rows']}
            ids = [row['context'] for row in plan['rows'] if row['status'] == 'eligible'] if all_eligible else list(selected)
            if not ids or len(ids) != len(set(ids)) or any(identity not in originals for identity in ids):
                raise ValueError('Select unique contexts from this plan')
            fresh = {row['context']: row for row in self._snapshot(list(originals))}
            for identity in ids:
                old, current = originals[identity], fresh[identity]
                previous = plan['results'].get(identity, {})
                if old['status'] not in ('eligible', 'needs_choice') or (all_eligible and old['status'] != 'eligible'):
                    result = {'status': 'conflict', 'reason': old['reason'] or 'not_eligible'}
                elif any(current[key] != old[key] for key in ('thread_id', 'desired_title', 'label', 'observed_label', 'binding')) or current['status'] == 'conflict':
                    result = {'status': 'conflict', 'reason': current['reason'] or 'plan_changed'}
                elif current['current_title'] == old['desired_title']:
                    result = {'status': 'already_matched', 'reason': 'verified_prior_write' if previous.get('status') == 'applied' else 'current_title_matches'}
                elif current['current_title'] != old['current_title']:
                    result = {'status': 'conflict', 'reason': 'title_changed'}
                elif not self.catalog.guarded_rename:
                    result = {'status': 'blocked', 'reason': 'provider_has_no_atomic_expected_title_rename'}
                else:
                    try:
                        outcome = self.catalog.rename_if_title(old['thread_id'], old['current_title'], old['desired_title'])
                    except (TimeoutError, ConnectionError):
                        outcome = 'unknown'
                    except (PermissionError, BlockingIOError):
                        outcome = 'blocked'
                    except Exception:
                        outcome = 'failed'
                    try:
                        observed = self.catalog.read(old['thread_id'])
                    except (OSError, sqlite3.Error, ValueError):
                        observed = None
                    if observed and observed.get('title') == old['desired_title']:
                        result = {'status': 'applied', 'reason': 'verified_readback'}
                    elif observed and observed.get('title') != old['current_title']:
                        result = {'status': 'conflict', 'reason': 'unexpected_readback'}
                    elif outcome == 'conflict':
                        result = {'status': 'conflict', 'reason': 'provider_expected_title_mismatch'}
                    elif outcome in ('unknown', 'applied'):
                        result = {'status': 'unknown', 'reason': 'write_not_verified'}
                    else:
                        result = {'status': outcome if outcome in ('blocked', 'failed') else 'failed', 'reason': 'provider_write_unavailable'}
                plan['results'][identity] = {'context': identity, 'thread_id': old['thread_id'],
                                             'previous_title': old['current_title'], 'desired_title': old['desired_title'],
                                             'checked_at': _now(), **result}
                _write(self.state / (plan_id + '.json'), plan)
            return self._public(plan)

    def undo(self, plan_id, context_id):
        """Restore one verified prior name only through the same atomic guard."""
        with _lock(self.state):
            plan = self._load_plan(plan_id)
            original = next((row for row in plan['rows'] if row['context'] == context_id), None)
            result = plan['results'].get(context_id)
            if original is None or result is None:
                raise ValueError('Select an exact applied row from this plan')
            fresh = self._snapshot([context_id])[0]
            if result['status'] not in ('applied', 'already_matched') or (result['status'] == 'already_matched' and result.get('reason') != 'verified_prior_write'):
                outcome = {'status': 'conflict', 'reason': 'no_verified_write'}
            elif any(fresh[key] != original[key] for key in ('thread_id', 'desired_title', 'binding')) or fresh['current_title'] != result['desired_title']:
                outcome = {'status': 'conflict', 'reason': 'title_or_mapping_changed'}
            elif not result['previous_title']:
                outcome = {'status': 'blocked', 'reason': 'blank_prior_title_requires_manual_reconciliation'}
            elif not self.catalog.guarded_rename:
                outcome = {'status': 'blocked', 'reason': 'provider_has_no_atomic_expected_title_rename'}
            else:
                try:
                    response = self.catalog.rename_if_title(result['thread_id'], result['desired_title'], result['previous_title'])
                except (TimeoutError, ConnectionError):
                    response = 'unknown'
                except (PermissionError, BlockingIOError):
                    response = 'blocked'
                except Exception:
                    response = 'failed'
                try:
                    readback = self.catalog.read(result['thread_id'])
                except (OSError, sqlite3.Error, ValueError):
                    readback = None
                if readback and readback.get('title') == result['previous_title']:
                    outcome = {'status': 'applied', 'reason': 'verified_readback'}
                elif readback and readback.get('title') != result['desired_title']:
                    outcome = {'status': 'conflict', 'reason': 'unexpected_readback'}
                else:
                    outcome = {'status': response if response in ('blocked', 'failed') else 'unknown',
                               'reason': 'undo_not_verified'}
            plan.setdefault('undo_results', {})[context_id] = {'checked_at': _now(), **outcome}
            _write(self.state / (plan_id + '.json'), plan)
            return self._public(plan)
