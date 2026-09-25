"""Explicit local code workspace bindings, preparation, and bounded resume."""
from __future__ import annotations

import contextlib
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

from .flow import _atomic_json, _find, _private, _registry, _writer_lock, load_profile, resolve
from .flow_tasks import inspect


def _store_path(profile: Path) -> Path:
    return profile.with_suffix('.code.json')


def _store(profile: Path) -> dict:
    path = _store_path(profile)
    if not path.exists():
        return {'version': 1, 'bindings': {}}
    _private(path, directory=False)
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('version') != 1 or not isinstance(data.get('bindings'), dict):
        raise ValueError('Invalid FLOW code workspace state')
    return data


def _item_id(reference: str, profile: Path, config: dict) -> str:
    item = _find(_registry(profile), resolve(reference, config)['canonical'])
    if not item:
        raise ValueError('Open the FLOW work item before binding a code repository')
    return item['id']


def _absolute(path: str | Path) -> Path:
    value = Path(path).expanduser()
    if not value.is_absolute() or value.is_symlink():
        raise ValueError('FLOW code paths must be absolute and nonsymlink')
    return value


def _git(root: Path, *args: str, check=True) -> subprocess.CompletedProcess:
    result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
                             '-c', 'core.attributesFile=/dev/null',
                             '-C', str(root), *args], capture_output=True, text=True, timeout=30,
                            check=False, env={**os.environ, 'GIT_TERMINAL_PROMPT': '0',
                                              'GIT_ATTR_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
                                              'GIT_CONFIG_NOSYSTEM': '1'})
    if len(result.stdout) > 4 * 1024 * 1024:
        raise ValueError('FLOW code Git metadata probe exceeds the 4 MiB limit')
    if check and result.returncode:
        raise ValueError(f'FLOW code Git {args[0]} failed; inspect the repository without resetting it')
    return result


@contextlib.contextmanager
def _repo_lock(path: Path):
    identity = hashlib.sha256(str(path.resolve(strict=False)).encode()).hexdigest()
    runtime = os.environ.get('XDG_RUNTIME_DIR')
    if runtime:
        base = Path(runtime)
        _private(base, directory=True)
    else:
        base = Path.home() / '.local' / 'state'
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
    private = base / 'starforge-ai-workbench'
    private.mkdir(mode=0o700, exist_ok=True)
    _private(private, directory=True)
    locks = private / 'flow-repo-locks'
    locks.mkdir(mode=0o700, exist_ok=True)
    _private(locks, directory=True)
    lock = locks / f'{identity}.lock'
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(fd).st_uid != os.getuid() or os.fstat(fd).st_mode & 0o077:
            raise ValueError('FLOW repository lock is not private')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('FLOW repository has another preparer; retry') from None
        yield
    finally:
        os.close(fd)


def bind(reference: str, *, name: str, repository: str | Path, worktree_root: str | Path,
         base_ref: str, branch: str | None = None, remote: str | None = None,
         allow_remote_read: bool = False, profile=None) -> dict:
    path, config = load_profile(profile)
    work_id = _item_id(reference, path, config)
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,39}', name):
        raise ValueError('Binding name must be simple lowercase text')
    if (not base_ref or base_ref.startswith('-') or '..' in base_ref or '@{' in base_ref
            or any(ch in base_ref for ch in ':~^?*[]\\{}')):
        raise ValueError('Select a simple explicit local base ref')
    repo = _absolute(repository)
    worktrees = _absolute(worktree_root)
    if repo.resolve(strict=False) == Path(config['root']).resolve():
        raise ValueError('Code repository must be separate from FLOW metadata history')
    if worktrees == repo or worktrees.is_relative_to(repo) or repo.is_relative_to(worktrees):
        raise ValueError('Code clone and worktree root must be separate directories')
    chosen_branch = branch or f'flow/{work_id}/{name}'
    if _git(repo, 'check-ref-format', '--branch', chosen_branch, check=False).returncode if repo.exists() else not re.fullmatch(r'[A-Za-z0-9_./-]+', chosen_branch):
        raise ValueError('Invalid work branch name')
    if remote:
        parsed = urlsplit(remote)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Remote clone URL must be an HTTPS URL without embedded credentials')
        if not allow_remote_read:
            raise ValueError('Remote clone needs explicit remote-read authorization')
    binding = {'name': name, 'repository': str(repo), 'worktree_root': str(worktrees),
               'base_ref': base_ref, 'branch': chosen_branch, 'remote': remote,
               'allow_remote_read': allow_remote_read, 'relation': None, 'pending': None}
    with _writer_lock(path):
        data = _store(path)
        entries = data['bindings'].setdefault(work_id, {})
        if name in entries:
            if {key: value for key, value in entries[name].items() if key not in {'relation', 'pending'}} != {
                    key: value for key, value in binding.items() if key not in {'relation', 'pending'}}:
                raise ValueError('Existing binding differs; inspect it instead of replacing it')
            return {'status': 'existing', 'work_item_id': work_id, 'binding': entries[name]}
        entries[name] = binding
        _atomic_json(_store_path(path), data)
    return {'status': 'bound', 'work_item_id': work_id, 'binding': binding}


def _worktrees(repo: Path) -> dict[str, str | None]:
    output = _git(repo, 'worktree', 'list', '--porcelain').stdout
    result = {}
    path = None
    branch = None
    for line in output.splitlines() + ['']:
        if not line:
            if path:
                result[path] = branch
            path = branch = None
        elif line.startswith('worktree '):
            path = line.removeprefix('worktree ')
        elif line.startswith('branch '):
            branch = line.removeprefix('branch refs/heads/')
    return result


def _head(path: Path) -> str:
    return _git(path, 'rev-parse', '--verify', 'HEAD').stdout.strip()


def _dirty(path: Path) -> bool:
    attribute_files = _git(path, 'ls-files', '-z', '--cached', '--others', '--exclude-standard',
                           '--', '.gitattributes', '*/.gitattributes').stdout.split('\x00')
    for name in attribute_files:
        if name:
            candidate = path / name
            if candidate.is_symlink():
                raise ValueError('Symlinked Git attributes need review before status inspection')
            if candidate.is_file() and re.search(r'(^|\s)filter=', candidate.read_text(encoding='utf-8'), re.MULTILINE):
                raise ValueError('Worktree Git filter attributes need review before status inspection')
    return bool(_git(path, 'status', '--porcelain', '--untracked-files=normal').stdout.strip())


def _checkout_safe(repo: Path, base: str) -> None:
    # A worktree checkout can execute configured filter processes. Inspect the
    # selected commit's tracked attributes and refuse filters rather than
    # treating Git's checkout as permission to execute arbitrary tooling.
    names = _git(repo, 'ls-tree', '-r', '-z', '--name-only', base).stdout.split('\x00')
    local_attributes = Path(_git(repo, 'rev-parse', '--git-path', 'info/attributes').stdout.strip())
    if local_attributes.exists() and re.search(r'(^|\s)filter=', local_attributes.read_text(encoding='utf-8'), re.MULTILINE):
        raise ValueError('Local Git filter attributes need explicit review before preparation')
    for name in names:
        if name == '.gitattributes' or name.endswith('/.gitattributes'):
            content = _git(repo, 'show', f'{base}:{name}').stdout
            if re.search(r'(^|\s)filter=', content, re.MULTILINE):
                raise ValueError('Tracked Git filter attributes need explicit review before preparation')


def _summary(reference: str, profile: Path) -> dict:
    try:
        tasks = inspect(reference, 'list', profile=profile)['tasks']
        recommendation = inspect(reference, 'next', profile=profile)
        return {'tasks': [{'task_id': row['task_id'], 'title': row['title'],
                           'unresolved_dependencies': row['unresolved_dependencies']} for row in tasks[:20]],
                'task_count': len(tasks), 'next_suggestion': recommendation['suggestion'],
                'task_limit': 20}
    except (ValueError, OSError) as exc:
        return {'tasks': 'unknown', 'reason': str(exc)}


def adopt(reference: str, *, name: str, worktree: str | Path, profile=None) -> dict:
    """Record an explicitly selected, verified existing worktree without claiming its owner."""
    path, config = load_profile(profile)
    work_id = _item_id(reference, path, config)
    selected = _absolute(worktree)
    with _writer_lock(path):
        data = _store(path)
        binding = deepcopy(data['bindings'].get(work_id, {}).get(name))
        if not binding:
            raise ValueError('Bind this repository before adopting an existing worktree')
        if binding.get('relation'):
            raise ValueError('This binding already has a recorded worktree; inspect it first')
    repo = Path(binding['repository'])
    with _repo_lock(repo):
        if not selected.is_dir() or selected.is_symlink():
            raise ValueError('Selected worktree is missing or unsafe')
        if _worktrees(repo).get(str(selected.resolve())) != binding['branch']:
            raise ValueError('Selected path is not a worktree on the bound branch')
        head = _head(selected)
        base = _git(repo, 'rev-parse', '--verify', f'{binding["base_ref"]}^{{commit}}').stdout.strip()
        ancestor = _git(repo, 'merge-base', '--is-ancestor', base, head, check=False).returncode == 0
        dirty = _dirty(selected)
        with _writer_lock(path):
            data = _store(path)
            current = data['bindings'].get(work_id, {}).get(name)
            if current != binding:
                raise ValueError('FLOW binding changed during adoption; inspect it again')
            current['relation'] = {'worktree': str(selected), 'branch': binding['branch'],
                'starting_commit': None, 'selected_base_commit': base, 'base_is_ancestor': ancestor,
                'head': head}
            current['pending'] = None
            _atomic_json(_store_path(path), data)
        return {'status': 'adopted', 'work_item_id': work_id, 'name': name,
                'worktree': str(selected), 'branch': binding['branch'], 'head': head,
                'selected_base_commit': base, 'base_is_ancestor': ancestor,
                'actual_starting_commit': 'unknown', 'dirty': dirty,
                'ownership': 'unknown', 'remote_freshness': 'unknown'}


def _one(binding: dict, work_id: str, *, create: bool, persist_pending=None) -> dict:
    repo = Path(binding['repository'])
    root = Path(binding['worktree_root'])
    name = binding['name']
    branch = binding['branch']
    target = root / f'{work_id}-{name}'
    relation = binding.get('relation')
    with (_repo_lock(repo) if create else contextlib.nullcontext()):
        if relation:
            recorded = Path(relation['worktree'])
            if not recorded.is_dir() or recorded.is_symlink():
                raise ValueError('Recorded worktree is missing or unsafe; reconcile without deleting it')
            if _git(recorded, 'symbolic-ref', '--short', 'HEAD', check=False).stdout.strip() != branch:
                raise ValueError('Recorded worktree branch differs; do not switch it automatically')
            if _worktrees(repo).get(str(recorded.resolve())) != branch:
                raise ValueError('Recorded worktree is not linked to the configured clone')
            head = _head(recorded)
            return {'status': 'resumed', 'name': name, 'repository': str(repo), 'worktree': str(recorded),
                    'branch': branch, 'head': head, 'starting_commit': relation['starting_commit'],
                    'recorded_head': relation['head'], 'diverged_from_record': head != relation['head'],
                    'selected_base_commit': relation.get('selected_base_commit', relation['starting_commit']),
                    'dirty': _dirty(recorded), 'ownership': 'unknown', 'remote_freshness': 'unknown'}
        if not create:
            return {'status': 'unprepared', 'name': name, 'next_action': 'Run work prepare explicitly'}
        if not repo.exists():
            if not binding.get('allow_remote_read') or not binding.get('remote'):
                raise ValueError('Configured clone is missing; authorize an explicit HTTPS remote read or provide a local clone')
            if repo.parent.is_symlink() or not repo.parent.is_dir():
                raise ValueError('Clone parent must already be a nonsymlink directory')
            result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', 'clone', '--no-checkout',
                binding['remote'], str(repo)], capture_output=True, text=True, timeout=120,
                env={**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GIT_CONFIG_GLOBAL': '/dev/null',
                     'GIT_CONFIG_NOSYSTEM': '1', 'GIT_ATTR_NOSYSTEM': '1'})
            if result.returncode:
                raise ValueError('Authorized remote clone failed; inspect the destination and retry')
        if _git(repo, 'rev-parse', '--is-bare-repository').stdout.strip() == 'true':
            raise ValueError('Bare clone is not supported for local FLOW preparation')
        base = _git(repo, 'rev-parse', '--verify', f'{binding["base_ref"]}^{{commit}}').stdout.strip()
        _checkout_safe(repo, base)
        if not binding.get('pending'):
            # A branch seen before our pending marker is ambiguous. Record the
            # exact base before Git creates either branch or worktree.
            if _git(repo, 'show-ref', '--verify', f'refs/heads/{branch}', check=False).returncode == 0:
                raise ValueError('Unrecorded work branch exists; explicit adoption is required')
            binding['pending'] = {'starting_commit': base}
            if persist_pending:
                persist_pending()
        assigned = _worktrees(repo)
        if target.exists():
            if target.is_symlink():
                raise ValueError('Worktree target is a symlink; manual reconciliation is required')
            if str(target.resolve()) not in assigned or assigned[str(target.resolve())] != branch:
                raise ValueError('Target path exists without a verified matching FLOW worktree')
            if not binding.get('pending'):
                raise ValueError('Matching unrecorded worktree needs explicit adoption/reconciliation')
            head = _head(target)
            if head != binding['pending']['starting_commit']:
                raise ValueError('Interrupted worktree changed before relation was recorded; reconcile')
            return {'status': 'recovered', 'name': name, 'repository': str(repo), 'worktree': str(target),
                    'branch': branch, 'head': head, 'starting_commit': binding['pending']['starting_commit'],
                    'dirty': _dirty(target), 'ownership': 'unknown', 'remote_freshness': 'unknown'}
        if any(value == branch for value in assigned.values()):
            raise ValueError('Work branch is already checked out elsewhere; ownership is uncertain')
        branch_exists = _git(repo, 'show-ref', '--verify', f'refs/heads/{branch}', check=False).returncode == 0
        if branch_exists and _git(repo, 'rev-parse', '--verify', f'refs/heads/{branch}^{{commit}}').stdout.strip() != binding['pending']['starting_commit']:
            raise ValueError('Pending work branch has moved; reconcile without changing it')
        if root.is_symlink() or not root.is_dir():
            raise ValueError('Worktree root must be a prepared nonsymlink directory')
        if branch_exists:
            _git(repo, 'worktree', 'add', '--', str(target), branch)
            starting = binding['pending']['starting_commit']
        else:
            _git(repo, 'worktree', 'add', '-b', branch, '--', str(target), base)
            starting = base
        return {'status': 'created', 'name': name, 'repository': str(repo), 'worktree': str(target),
                'branch': branch, 'head': _head(target), 'starting_commit': starting,
                'dirty': _dirty(target), 'ownership': 'local-preparation', 'remote_freshness': 'unknown'}


def workspace(reference: str, *, profile=None, create: bool = False) -> dict:
    path, config = load_profile(profile)
    work_id = _item_id(reference, path, config)
    with (_writer_lock(path) if create else contextlib.nullcontext()):
        data = _store(path)
        names = sorted(data['bindings'].get(work_id, {}))
        if not names:
            raise ValueError('No explicit repository binding; use work bind before preparation')
    results = []
    for name in names:
        try:
            with (_writer_lock(path) if create else contextlib.nullcontext()):
                binding = deepcopy(_store(path)['bindings'][work_id][name])
            def persist_pending() -> None:
                with _writer_lock(path):
                    latest = _store(path)
                    current = latest['bindings'][work_id][name]
                    if (current.get('relation') or current.get('pending') or
                            {key: value for key, value in current.items() if key not in {'relation', 'pending'}} !=
                            {key: value for key, value in binding.items() if key not in {'relation', 'pending'}}):
                        raise ValueError('FLOW binding changed during preparation; retry after inspection')
                    current['pending'] = binding['pending']
                    _atomic_json(_store_path(path), latest)
            result = _one(binding, work_id, create=create, persist_pending=persist_pending)
            if result['status'] in {'created', 'recovered'}:
                with _writer_lock(path):
                    latest = _store(path)
                    current = latest['bindings'][work_id][name]
                    if current.get('relation') or current.get('pending') != binding.get('pending'):
                        raise ValueError('FLOW relation changed during preparation; inspect existing worktree')
                    current['relation'] = {'worktree': result['worktree'], 'branch': result['branch'],
                        'starting_commit': result['starting_commit'], 'head': result['head']}
                    current['pending'] = None
                    _atomic_json(_store_path(path), latest)
            results.append(result)
        except (OSError, ValueError, subprocess.SubprocessError, KeyError) as exc:
            results.append({'status': 'error', 'name': name, 'reason': str(exc),
                            'next_action': 'Inspect this repository and retry; successful siblings remain intact'})
    return {'work_item_id': work_id, 'repositories': results, 'local_context': _summary(reference, path),
            'linked_prs': 'unavailable without tracker enrichment', 'agent_session': 'not started',
            'next_authorized_step': 'Review the local task suggestion and workspace state before coding'}
