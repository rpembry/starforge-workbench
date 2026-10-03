"""Explicit, exact-ID Codex title reconciliation with disclosed write modes."""
from __future__ import annotations

import fcntl
from copy import deepcopy
import json
import os
import select
import shutil
import subprocess
from pathlib import Path
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
THREAD_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,36}$")
MAX_TITLE = 200


def _now():
    return datetime.now(timezone.utc).isoformat()


def _private_dir(path: Path):
    missing = []
    current = path
    while not current.exists():
        if current.is_symlink():
            raise ValueError('Unsafe reconciliation state directory')
        missing.append(current)
        current = current.parent
    if current.is_symlink():
        raise ValueError('Unsafe reconciliation state directory')
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        parent_fd = os.open(directory.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    if path.is_symlink():
        raise ValueError('Unsafe reconciliation state directory')
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
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
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
    """Read metadata and use only Codex app-server for explicit renames."""

    guarded_rename = False
    practical_rename = True

    def __init__(self, home: Path | None = None, executable: str | None = None):
        self.home = home or Path.home()
        self.codex_root = Path(os.environ.get('CODEX_HOME') or self.home / '.codex').expanduser().resolve()
        self.executable = executable or shutil.which('codex')

    def read(self, thread_id: str):
        if not THREAD_ID.fullmatch(thread_id):
            return None
        rows = []
        for path in self.codex_root.glob('state*.sqlite'):
            uri = path.resolve().as_uri() + '?mode=ro'
            with sqlite3.connect(uri, uri=True, timeout=2) as db:
                # Codex app-server thread/name/set persists the chosen display
                # name separately from the generated title in 0.155.1.
                columns = {item[1] for item in db.execute('PRAGMA table_info(threads)')}
                effective_title = "COALESCE(NULLIF(name, ''), title)" if 'name' in columns else 'title'
                row = db.execute(f'SELECT id, cwd, {effective_title}, archived, source FROM threads WHERE id=?',
                                 (thread_id,)).fetchone()
                if row:
                    rows.append(row)
        if len(rows) != 1:
            return None
        identity, cwd, title, archived, source = rows[0]
        return {'id': identity, 'cwd': cwd, 'title': title, 'archived': bool(archived), 'source': source}

    def rename_if_title(self, thread_id: str, expected: str, desired: str):
        raise NotImplementedError('Codex 0.155.1 has no atomic expected-title rename')

    def rename_practical(self, thread_id: str, desired: str):
        """Issue one supported thread/name/set request on a fresh owned app-server."""
        if not self.executable or not THREAD_ID.fullmatch(thread_id):
            return 'blocked'
        try:
            process = subprocess.Popen([self.executable, 'app-server', '--stdio'],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, bufsize=0,
                                       env={**os.environ, 'CODEX_HOME': str(self.codex_root)})
        except OSError:
            return 'blocked'
        pending = b''
        def request(identity, method, params):
            nonlocal pending
            process.stdin.write((json.dumps({'jsonrpc': '2.0', 'id': identity, 'method': method,
                                             'params': params}, ensure_ascii=False) + '\n').encode())
            process.stdin.flush()
            deadline = time.monotonic() + 8
            while True:
                if b'\n' in pending:
                    line, pending = pending.split(b'\n', 1)
                    message = json.loads(line)
                    if message.get('id') == identity:
                        return message
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
                    raise TimeoutError('Codex app-server acknowledgement unavailable')
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    raise ConnectionError('Codex app-server closed before acknowledgement')
                pending += chunk
        try:
            try:
                initialized = request(1, 'initialize', {'clientInfo': {'name': 'starforge-workbench', 'version': '0.2.0'}})
            except (TimeoutError, ConnectionError, OSError):
                return 'blocked'  # no rename request has been sent
            if 'error' in initialized or 'result' not in initialized:
                return 'blocked'
            process.stdin.write((json.dumps({'jsonrpc': '2.0', 'method': 'initialized', 'params': {}}) + '\n').encode())
            process.stdin.flush()
            response = request(2, 'thread/name/set', {'threadId': thread_id, 'name': desired})
            if 'error' in response or 'result' not in response:
                raise ConnectionError('Codex rename outcome is not confirmed')
            return 'applied'
        finally:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)


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
            if not reason and (self.state / 'uncertain' / (thread_id + '.json')).exists():
                reason = 'unresolved_provider_outcome'
            rows.append({'context': context_id, 'label': desired, 'observed_label': observed,
                         'thread_id': thread_id, 'current_title': record.get('title') if record else None,
                         'desired_title': desired, 'binding': binding,
                         'provider_root': str(self.catalog.codex_root) if hasattr(self.catalog, 'codex_root') else None,
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
            row.pop('provider_root', None)
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

    def _mark_unknown(self, thread_id, plan_id):
        _write(self.state / 'uncertain' / (thread_id + '.json'),
               {'thread_id': thread_id, 'plan_id': plan_id, 'recorded_at': _now(),
                'reason': 'write_intent_or_unresolved_provider_outcome'})

    def _clear_intent(self, thread_id):
        path = self.state / 'uncertain' / (thread_id + '.json')
        path.unlink(missing_ok=True)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def status(self, plan_id):
        return self._public(self._load_plan(plan_id))

    def apply(self, plan_id, selected=None, all_eligible=False, mode='strict', confirm_non_atomic=False):
        if bool(selected) == bool(all_eligible):
            raise ValueError('Select exact context IDs or all eligible rows')
        if mode not in ('strict', 'practical') or (mode == 'practical' and not confirm_non_atomic):
            raise ValueError('Practical mode requires explicit non-atomic confirmation')
        with _lock(self.state):
            plan = self._load_plan(plan_id)
            originals = {row['context']: row for row in plan['rows']}
            ids = [row['context'] for row in plan['rows'] if row['status'] == 'eligible'] if all_eligible else list(selected)
            if not ids or len(ids) != len(set(ids)) or any(identity not in originals for identity in ids):
                raise ValueError('Select unique contexts from this plan')
            for identity in ids:
                sent = False
                old = originals[identity]
                try:
                    current = self._snapshot([identity])[0]
                except (ValueError, KeyError, OSError, sqlite3.Error):
                    current = None
                evidence = plan.setdefault('verified_writes', {}).get(identity)
                if old['status'] not in ('eligible', 'needs_choice') or (all_eligible and old['status'] != 'eligible'):
                    result = {'status': 'conflict', 'reason': old['reason'] or 'not_eligible'}
                elif current is None:
                    result = {'status': 'conflict', 'reason': 'context_or_mapping_unavailable'}
                elif any(current[key] != old[key] for key in ('thread_id', 'desired_title', 'label', 'observed_label', 'binding', 'provider_root')) or current['status'] == 'conflict':
                    result = {'status': 'conflict', 'reason': current['reason'] or 'plan_changed'}
                elif current['current_title'] == old['desired_title']:
                    result = {'status': 'already_matched', 'reason': 'verified_prior_write' if evidence else 'current_title_matches'}
                elif current['current_title'] != old['current_title']:
                    result = {'status': 'conflict', 'reason': 'title_changed'}
                elif mode == 'strict' and not self.catalog.guarded_rename:
                    result = {'status': 'blocked', 'reason': 'provider_has_no_atomic_expected_title_rename'}
                elif mode == 'practical' and not getattr(self.catalog, 'practical_rename', False):
                    result = {'status': 'blocked', 'reason': 'supported_provider_rename_unavailable'}
                else:
                    # Durable intent comes before any request that might outlive its acknowledgement.
                    self._mark_unknown(old['thread_id'], plan_id)
                    sent = True
                    try:
                        outcome = (self.catalog.rename_if_title(old['thread_id'], old['current_title'], old['desired_title'])
                                   if mode == 'strict' else self.catalog.rename_practical(old['thread_id'], old['desired_title']))
                    except (TimeoutError, ConnectionError):
                        outcome = 'unknown'
                    except (PermissionError, BlockingIOError):
                        outcome = 'blocked'
                    except Exception:
                        outcome = 'unknown'
                    try:
                        observed = self.catalog.read(old['thread_id'])
                    except (OSError, sqlite3.Error, ValueError):
                        observed = None
                    if outcome == 'applied' and observed and observed.get('title') == old['desired_title']:
                        result = {'status': 'applied', 'reason': 'verified_readback'}
                        plan['verified_writes'][identity] = {'thread_id': old['thread_id'],
                                                              'previous_title': old['current_title'],
                                                              'written_title': old['desired_title'],
                                                              'mode': mode, 'verified_at': _now()}
                    elif outcome == 'conflict':
                        result = {'status': 'conflict', 'reason': 'provider_expected_title_mismatch'}
                    elif outcome == 'unknown':
                        result = {'status': 'unknown', 'reason': 'provider_outcome_unresolved'}
                    elif observed and observed.get('title') != old['current_title']:
                        result = {'status': 'conflict', 'reason': 'unexpected_readback'}
                    elif outcome == 'applied':
                        result = {'status': 'unknown', 'reason': 'write_not_verified'}
                    else:
                        result = {'status': outcome if outcome in ('blocked', 'failed') else 'failed', 'reason': 'provider_write_unavailable'}
                plan['results'][identity] = {'context': identity, 'thread_id': old['thread_id'],
                                             'previous_title': old['current_title'], 'desired_title': old['desired_title'],
                                             'checked_at': _now(), 'mode': mode, **result}
                _write(self.state / (plan_id + '.json'), plan)
                if sent and result['status'] != 'unknown':
                    self._clear_intent(old['thread_id'])
            return self._public(plan)

    def undo(self, plan_id, context_id, mode='strict', confirm_non_atomic=False):
        """Restore one verified prior name with the selected disclosed guard."""
        if mode not in ('strict', 'practical') or (mode == 'practical' and not confirm_non_atomic):
            raise ValueError('Practical mode requires explicit non-atomic confirmation')
        with _lock(self.state):
            sent = False
            plan = self._load_plan(plan_id)
            original = next((row for row in plan['rows'] if row['context'] == context_id), None)
            evidence = plan.get('verified_writes', {}).get(context_id)
            if original is None:
                raise ValueError('Select an exact row from this plan')
            try:
                fresh = self._snapshot([context_id])[0]
            except (ValueError, KeyError, OSError, sqlite3.Error):
                fresh = None
            if not evidence:
                outcome = {'status': 'conflict', 'reason': 'no_verified_write'}
            elif fresh is None or fresh['status'] == 'conflict' or any(fresh[key] != original[key] for key in ('thread_id', 'desired_title', 'binding', 'provider_root')) or fresh['current_title'] != evidence['written_title']:
                outcome = {'status': 'conflict', 'reason': 'title_or_mapping_changed'}
            elif not evidence['previous_title']:
                outcome = {'status': 'blocked', 'reason': 'blank_prior_title_requires_manual_reconciliation'}
            elif mode == 'strict' and not self.catalog.guarded_rename:
                outcome = {'status': 'blocked', 'reason': 'provider_has_no_atomic_expected_title_rename'}
            elif mode == 'practical' and not getattr(self.catalog, 'practical_rename', False):
                outcome = {'status': 'blocked', 'reason': 'supported_provider_rename_unavailable'}
            else:
                self._mark_unknown(evidence['thread_id'], plan_id)
                sent = True
                try:
                    response = (self.catalog.rename_if_title(evidence['thread_id'], evidence['written_title'], evidence['previous_title'])
                                if mode == 'strict' else self.catalog.rename_practical(evidence['thread_id'], evidence['previous_title']))
                except (TimeoutError, ConnectionError):
                    response = 'unknown'
                except (PermissionError, BlockingIOError):
                    response = 'blocked'
                except Exception:
                    response = 'unknown'
                try:
                    readback = self.catalog.read(evidence['thread_id'])
                except (OSError, sqlite3.Error, ValueError):
                    readback = None
                if response == 'applied' and readback and readback.get('title') == evidence['previous_title']:
                    outcome = {'status': 'applied', 'reason': 'verified_readback'}
                    plan['verified_writes'].pop(context_id, None)
                elif response == 'conflict':
                    outcome = {'status': 'conflict', 'reason': 'provider_expected_title_mismatch'}
                elif response == 'unknown':
                    outcome = {'status': 'unknown', 'reason': 'provider_outcome_unresolved'}
                elif readback and readback.get('title') != evidence['written_title']:
                    outcome = {'status': 'conflict', 'reason': 'unexpected_readback'}
                else:
                    outcome = {'status': response if response in ('blocked', 'failed') else 'unknown',
                               'reason': 'undo_not_verified'}
            plan.setdefault('undo_results', {})[context_id] = {'checked_at': _now(), **outcome}
            _write(self.state / (plan_id + '.json'), plan)
            if sent and outcome['status'] != 'unknown':
                self._clear_intent(evidence['thread_id'])
            return self._public(plan)
