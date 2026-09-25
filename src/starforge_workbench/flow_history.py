"""Serialized, narrow Git checkpoints for a private FLOW metadata repository."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from .flow import (_atomic_json, _find, _private, _registry,
                   _safe_components, _writer_lock, load_profile, resolve)


def _run(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True,
                            timeout=20, check=False)
    if check and result.returncode:
        raise ValueError(f'FLOW Git {args[0]} failed; inspect metadata repository hooks, signing, and index before retrying')
    return result


def revision(root: Path) -> str:
    result = _run(root, 'rev-parse', '--verify', 'HEAD', check=False)
    return result.stdout.strip() if result.returncode == 0 else 'UNBORN'


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _state(profile: Path) -> dict:
    path = profile.with_suffix('.operations.json')
    if not path.exists():
        return {'version': 1, 'operations': {}}
    _private(path, directory=False)
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('version') != 1 or not isinstance(data.get('operations'), dict):
        raise ValueError('FLOW recovery state is invalid')
    return data


def _save(profile: Path, state: dict) -> None:
    _atomic_json(profile.with_suffix('.operations.json'), state)


def _history_guard(root: Path, profile: Path) -> None:
    committed = [entry['result']['checkpoint'] for entry in _state(profile)['operations'].values()
                 if entry['status'] == 'committed']
    if not committed:
        return
    head = revision(root)
    if head == 'UNBORN' or _run(root, 'merge-base', '--is-ancestor', committed[-1], head,
                                check=False).returncode != 0:
        raise ValueError('FLOW retained checkpoint history is missing or rewritten; reconcile before writing')


def _item(root: Path, profile: Path, config: dict, reference: str) -> tuple[dict, Path, str]:
    item = _find(_registry(profile), resolve(reference, config)['canonical'])
    if not item:
        raise ValueError('Open this work item before changing its tasks')
    relative = item['relative'] + '/TASKS.md'
    target = _safe_components(root, relative)
    if target.is_symlink() or not target.is_file():
        raise ValueError('Registered TASKS.md is missing or unsafe')
    current = target.read_text(encoding='utf-8')
    if (f'<!-- FLOW work-item: {item["id"]} -->' not in current
            or f'<!-- Source: {item["source"]} -->' not in current):
        raise ValueError('TASKS.md identity differs from the registry')
    return item, target, relative


def snapshot(reference: str, *, profile=None) -> dict:
    path, config = load_profile(profile)
    root = Path(config['root'])
    _history_guard(root, path)
    item, target, _ = _item(root, path, config, reference)
    content = target.read_bytes()
    return {'work_item_id': item['id'], 'revision': revision(root),
            'document_hash': digest(content), 'document': content.decode('utf-8')}


def operation_state(reference: str, operation_id: str, *, profile=None) -> dict | None:
    path, config = load_profile(profile)
    item, _, _ = _item(Path(config['root']), path, config, reference)
    entry = _state(path)['operations'].get(operation_id)
    if entry and entry['work_item_id'] != item['id']:
        raise ValueError('Operation ID belongs to another work item')
    return entry


def _clean_index(root: Path) -> None:
    result = _run(root, 'diff', '--cached', '--name-only')
    if result.stdout.strip():
        raise ValueError('FLOW index contains staged content; commit or unstage it explicitly before retrying')


def _assert_retained_branch(root: Path) -> None:
    if _run(root, 'symbolic-ref', '--short', 'HEAD').stdout.strip() != 'flow-history':
        raise ValueError('FLOW metadata history must remain on the retained flow-history branch')


def _pending_index(root: Path, relative: str) -> None:
    staged = _run(root, 'diff', '--cached', '--name-only').stdout.splitlines()
    if any(path not in {relative, '.gitignore'} for path in staged):
        raise ValueError('Unrelated staged content prevents FLOW recovery')


def _committed_document(root: Path, relative: str) -> bytes | None:
    result = subprocess.run(['git', '-C', str(root), 'show', f'HEAD:{relative}'],
                            capture_output=True, timeout=20, check=False)
    return result.stdout if result.returncode == 0 else None


def _commit(root: Path, relative: str, message: str) -> str:
    # Stage only allowlisted metadata documents. Normal `git commit` executes local
    # hooks and signing; no temporary index or hook override hides repository policy.
    paths = [relative]
    if revision(root) == 'UNBORN':
        paths.insert(0, '.gitignore')
    _run(root, 'add', '--', *paths)
    _run(root, 'commit', '-m', message, '--only', '--', *paths)
    return revision(root)


def _replace(target: Path, content: bytes) -> None:
    temporary = target.with_name('.TASKS.md.flow-tmp')
    if temporary.exists() or temporary.is_symlink():
        raise ValueError('FLOW temporary document exists; reconcile it before retrying')
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def _outcome(message: str, item_id: str, operation_id: str, task_id: str | None,
             actor: str, evidence: str) -> str:
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', operation_id):
        raise ValueError('Operation ID must be stable kebab-case')
    if task_id is not None and not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', task_id):
        raise ValueError('Task ID must be stable kebab-case')
    if (not actor.strip() or len(actor) > 120 or len(evidence) > 500 or
            any(character in value for value in (actor, evidence) for character in '\r\n')):
        raise ValueError('Actor/evidence must be concise single-line text')
    return (f'FLOW {message}\n\nFLOW-Operation: {operation_id}\nFLOW-Work-Item: {item_id}'
            + (f'\nFLOW-Task: {task_id}' if task_id else '')
            + f'\nFLOW-Actor: {actor}\nFLOW-Evidence: {evidence}')


def mutate_document(reference: str, *, operation_id: str, expected_revision: str,
                    expected_hash: str, proposed: str, operation: str, actor: str,
                    task_id: str | None = None, evidence: str = '', profile=None,
                    checkpoint_pending: bool = False, client_hash: str | None = None) -> dict:
    """Commit an exact proposed document; completion gets a pre-removal checkpoint."""
    if operation not in {'add', 'update', 'block', 'complete', 'cancel', 'supersede', 'reopen', 'assign-id'}:
        raise ValueError('Unsupported FLOW operation')
    request_hash = digest(json.dumps([expected_revision, expected_hash, proposed, operation,
        actor, task_id, evidence], ensure_ascii=False).encode('utf-8'))
    path, config = load_profile(profile)
    root = Path(config['root'])
    with _writer_lock(path):
        _assert_retained_branch(root)
        _history_guard(root, path)
        item, target, relative = _item(root, path, config, reference)
        state = _state(path)
        previous = state['operations'].get(operation_id)
        if previous:
            if previous['work_item_id'] != item['id'] or previous['operation'] != operation:
                raise ValueError('Operation ID was previously used for a different mutation')
            if previous.get('request_hash') != request_hash:
                raise ValueError('Operation ID was previously used for different content')
            # Early version-1 journal entries omit client_hash. A direct caller
            # still supplies None, while request_hash above verifies its bytes.
            if previous.get('client_hash') != client_hash:
                raise ValueError('Operation ID was previously used for different command arguments')
            if previous['status'] == 'committed':
                return previous['result']
            if (previous['expected_revision'] != expected_revision or
                    previous['before_hash'] != expected_hash or
                    previous['after_hash'] != digest(proposed.encode('utf-8')) or
                    previous.get('task_id') != task_id):
                raise ValueError('Pending operation differs from this request; inspect recovery state')
            commit_message = _run(root, 'log', '-1', '--format=%B', check=False).stdout
            if (f'FLOW-Operation: {operation_id}\n' in commit_message
                    and digest(target.read_bytes()) == previous['after_hash']):
                head = revision(root)
                result = {'status': 'committed', 'operation': operation,
                          'work_item_id': item['id'], 'task_id': previous.get('task_id'),
                          'before_revision': previous['expected_revision'], 'checkpoint': head,
                          'definition_checkpoint': previous.get('definition_checkpoint'),
                          'after_revision': head, 'document_hash': previous['after_hash'],
                          'publication': 'pending'}
                state['operations'][operation_id] = {'status': 'committed', 'operation': operation,
                    'work_item_id': item['id'], 'request_hash': request_hash, 'client_hash': client_hash, 'result': result}
                _save(path, state)
                return result
            _pending_index(root, relative)
            head = revision(root)
            before = target.read_bytes()
            if (operation in {'complete', 'cancel', 'supersede'} and
                    previous.get('definition_checkpoint') is None and
                    f'FLOW-Operation: {operation_id}-definition\n' in commit_message and
                    digest(before) == previous['before_hash'] and
                    _committed_document(root, relative) == before):
                parent = _run(root, 'rev-parse', 'HEAD^', check=False).stdout.strip() or 'UNBORN'
                if parent == expected_revision:
                    previous['definition_checkpoint'] = head
                    _save(path, state)
            if head in {expected_revision, previous.get('definition_checkpoint')} and digest(before) == previous['after_hash']:
                commit_id = _commit(root, relative, _outcome(operation, item['id'], operation_id, task_id, actor, evidence))
                result = {'status': 'committed', 'operation': operation, 'work_item_id': item['id'],
                    'task_id': task_id, 'before_revision': expected_revision, 'checkpoint': commit_id,
                    'definition_checkpoint': previous.get('definition_checkpoint'),
                    'after_revision': commit_id, 'document_hash': previous['after_hash'], 'publication': 'pending'}
                state['operations'][operation_id] = {'status': 'committed', 'operation': operation,
                    'work_item_id': item['id'], 'request_hash': request_hash, 'client_hash': client_hash, 'result': result}
                _save(path, state)
                return result
            if (head == previous.get('definition_checkpoint') and
                    digest(before) == previous['before_hash'] and
                    operation in {'complete', 'cancel', 'supersede'}):
                _replace(target, proposed.encode('utf-8'))
                commit_id = _commit(root, relative, _outcome(operation, item['id'], operation_id, task_id, actor, evidence))
                result = {'status': 'committed', 'operation': operation, 'work_item_id': item['id'],
                    'task_id': task_id, 'before_revision': expected_revision, 'checkpoint': commit_id,
                    'definition_checkpoint': head, 'after_revision': commit_id,
                    'document_hash': previous['after_hash'], 'publication': 'pending'}
                state['operations'][operation_id] = {'status': 'committed', 'operation': operation,
                    'work_item_id': item['id'], 'request_hash': request_hash, 'client_hash': client_hash, 'result': result}
                _save(path, state)
                return result
            if head == expected_revision and digest(before) == previous['before_hash']:
                # No document or Git boundary was crossed. Clear the journal and
                # revalidate the full request under this same writer lock.
                del state['operations'][operation_id]
                _save(path, state)
            else:
                raise ValueError('FLOW operation has an incomplete checkpoint; reconcile its document and Git history')
        if any(entry['status'] != 'committed' for entry in state['operations'].values()):
            raise ValueError('Another FLOW operation needs recovery before new writes')
        _clean_index(root)
        before = target.read_bytes()
        head = revision(root)
        if head != expected_revision or digest(before) != expected_hash:
            raise ValueError('Stale FLOW revision or document hash; preview again')
        if f'<!-- FLOW work-item: {item["id"]} -->' not in proposed or f'<!-- Source: {item["source"]} -->' not in proposed:
            raise ValueError('Proposed document must preserve its identity preamble')
        after = proposed.encode('utf-8')
        if after == before:
            raise ValueError('No FLOW document change to checkpoint')
        committed = _committed_document(root, relative)
        terminal = operation in {'complete', 'cancel', 'supersede'}
        initial = (committed is None and before.decode('utf-8') ==
                   f'# Tasks\n\n<!-- FLOW work-item: {item["id"]} -->\n<!-- Source: {item["source"]} -->\n')
        if committed != before and head != 'UNBORN' and not (checkpoint_pending or initial):
            raise ValueError('TASKS.md has uncheckpointed edits; preview and explicitly checkpoint them first')
        if terminal and (not task_id or f'**ID**: {task_id}' not in before.decode('utf-8')):
            raise ValueError('Terminal operation needs a present stable task ID')
        if terminal and f'**ID**: {task_id}' in proposed:
            raise ValueError('Terminal operation must remove the selected task ID')
        message = _outcome(operation, item['id'], operation_id, task_id, actor, evidence)
        state['operations'][operation_id] = {'status': 'pending', 'operation': operation,
            'work_item_id': item['id'], 'before_hash': digest(before), 'after_hash': digest(after),
            'expected_revision': head, 'task_id': task_id, 'request_hash': request_hash,
            'client_hash': client_hash}
        _save(path, state)
        try:
            definition_commit = None
            if terminal and committed != before:
                definition_commit = _commit(root, relative, _outcome('definition', item['id'],
                    operation_id + '-definition', task_id, actor, 'current definition before removal'))
                state['operations'][operation_id]['definition_checkpoint'] = definition_commit
                _save(path, state)
            _replace(target, after)
            commit_id = _commit(root, relative, message)
        except (OSError, ValueError, subprocess.SubprocessError):
            # Keep pending state and authored bytes for explicit, non-destructive recovery.
            raise
        result = {'status': 'committed', 'operation': operation, 'work_item_id': item['id'],
                  'task_id': task_id, 'before_revision': head, 'checkpoint': commit_id,
                  'definition_checkpoint': definition_commit, 'after_revision': commit_id,
                  'document_hash': digest(after), 'publication': 'pending'}
        state['operations'][operation_id] = {'status': 'committed', 'operation': operation,
            'work_item_id': item['id'], 'request_hash': request_hash, 'client_hash': client_hash,
            'result': result}
        _save(path, state)
        return result
