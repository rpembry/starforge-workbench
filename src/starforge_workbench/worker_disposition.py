"""Review-bound archival of stopped worker allocations; archived data is retained."""
import hashlib
import json
import os
from pathlib import Path
import stat

from . import docker_worker as w

MAX_BYTES = 128 * 1024 * 1024
MAX_ENTRIES = 10000
ROOTS = ('worktree', 'scratch', 'artifacts')


def hash_bytes(data):
    return hashlib.sha256(data).hexdigest()


def inventory(roots):
    result = {}
    total = 0
    for label, root in roots.items():
        if not root.exists() and not root.is_symlink(): continue
        def walk(path):
            yield path
            if stat.S_ISDIR(path.lstat().st_mode):
                with os.scandir(path) as children:
                    for child in children:
                        yield from walk(Path(child.path))
        for path in walk(root):
            info = path.lstat()
            name = label + ('/'+str(path.relative_to(root)) if path != root else '')
            if stat.S_ISDIR(info.st_mode):
                result[name] = {'kind':'directory', 'mode':stat.S_IMODE(info.st_mode)}
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                total += info.st_size
                if total > MAX_BYTES: raise w.WorkerError('Review exceeds archive size limit')
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd,'rb') as stream:
                    opened=os.fstat(stream.fileno())
                    if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                        raise w.WorkerError('File type changed during review')
                    data = stream.read(MAX_BYTES+1)
                    after = os.fstat(stream.fileno())
                    if len(data) != info.st_size or any(getattr(after,k) != getattr(info,k) for k in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')):
                        raise w.WorkerError('File changed during review')
                result[name] = {'kind':'file','mode':stat.S_IMODE(info.st_mode),
                                'bytes':len(data),'sha256':hash_bytes(data)}
            else:
                raise w.WorkerError('Archive refuses symlinks, hardlinks and special files')
            if len(result) > MAX_ENTRIES: raise w.WorkerError('Review has too many entries')
    return result


def fsync_tree(root):
    for path in [*root.rglob('*'), root]:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            mode=os.fstat(fd).st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise w.WorkerError('Archive file type changed; content retained')
            os.fsync(fd)
        finally: os.close(fd)


def read_private(path):
    if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise w.WorkerError('Unsafe disposition metadata')
    return json.loads(path.read_text())


def ensure_stopped(receipt, sudo):
    if receipt['phase'] not in ('exited','cancelled','start_failed') or receipt.get('artifacts') != 'complete':
        raise w.WorkerError('Recover terminal status and complete artifacts before review')
    if w.inspect_container(w.docker_prefix(sudo), receipt) is not None:
        raise w.WorkerError('Recover the owned container before review or disposition')


def verify_unstaged(receipt, tree=None):
    if w.git(tree or receipt['worktree'], 'diff', '--cached', '--binary', '--no-ext-diff', '--no-textconv', 'HEAD'):
        raise w.WorkerError('Staged changes are unsupported; retain this worktree for review')


def review(attempt, sudo=False):
    path, receipt = w.load_attempt(attempt)
    with w.attempt_lock(path):
        if (path/'disposition.json').exists():
            raise w.WorkerError('Disposition already started; retry its approved token')
        ensure_stopped(receipt,sudo)
        w.verify_worktree(receipt)
        verify_unstaged(receipt)
        entries = inventory({name:path/name for name in ROOTS})
        data = dict(attempt_id=receipt['attempt_id'],token=receipt['token'],
                    repository=receipt['repository'],revision=receipt['revision'],entries=entries)
        w.atomic(path/'review.json',data)
        return dict(attempt_id=receipt['attempt_id'], review_sha256=hash_bytes((path/'review.json').read_bytes()),
                    review_manifest=str(path/'review.json'), entries=len(entries),
                    next_action='Review this snapshot, then dispose with its exact review_sha256')


def dispose(attempt, review_sha256, sudo=False):
    path, receipt = w.load_attempt(attempt)
    with w.attempt_lock(path):
        approved = read_private(path/'review.json')
        if hash_bytes((path/'review.json').read_bytes()) != review_sha256:
            raise w.WorkerError('Review token does not match')
        if any(approved[key] != receipt[key] for key in ('attempt_id','token','repository','revision')):
            raise w.WorkerError('Review ownership changed')
        journal_path = path/'disposition.json'
        journal = read_private(journal_path) if journal_path.exists() else None
        archive = path/'archive'
        if journal and journal.get('phase') not in ('archiving','complete'):
            raise w.WorkerError('Unknown disposition phase')
        if journal and journal.get('review_sha256') != review_sha256:
            raise w.WorkerError('Disposition review token changed')
        if not journal:
            ensure_stopped(receipt,sudo)
            w.verify_worktree(receipt)
            verify_unstaged(receipt)
            if inventory({name:path/name for name in ROOTS}) != approved['entries']:
                raise w.WorkerError('Work changed since review; prepare a fresh review')
            if archive.exists() or archive.is_symlink():
                raise w.WorkerError('Unexpected archive path')
            journal = {'review_sha256':review_sha256,'phase':'archiving'}
            w.atomic(journal_path,journal)  # durable intent before any rename
        if journal['phase'] == 'complete' and any((path/name).exists() or (path/name).is_symlink() for name in ('worktree','scratch')):
            raise w.WorkerError('Disposed allocation reappeared; nothing removed')
        if archive.is_symlink(): raise w.WorkerError('Archive path changed')
        archive.mkdir(mode=0o700,exist_ok=True)
        w.private_directory(archive)
        roots = {}
        for name in ROOTS:
            source, target = path/name, archive/name
            # Artifacts stay at their existing public receipt path.
            if name == 'artifacts': roots[name] = source; continue
            if source.exists() and target.exists():
                raise w.WorkerError('Ambiguous archive ownership; both allocations exist')
            roots[name] = target if target.exists() else source
        if inventory(roots) != approved['entries']:
            raise w.WorkerError('Reviewed/archive content changed; all data retained')
        if journal['phase'] != 'complete':
            ensure_stopped(receipt,sudo)
            if (path/'worktree').exists(): w.verify_worktree(receipt)
            for name in ('worktree','scratch'):
                source, target = path/name, archive/name
                if source.exists(): os.rename(source,target)
            # Preserve original bytes rather than copy then delete. Open writers
            # would still target retained archive files; revalidation refuses drift.
            fsync_tree(archive)
            fsync_tree(path/'artifacts')
            fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
            try: os.fsync(fd)
            finally: os.close(fd)
            roots = {name:(path/name if name=='artifacts' else archive/name) for name in ROOTS}
            if inventory(roots) != approved['entries']:
                raise w.WorkerError('Archive changed during disposition; data retained')
            if (path/'worktree').exists() or (path/'worktree').is_symlink():
                raise w.WorkerError('Worktree path reappeared; nothing deleted')
            listing=w.git(receipt['repository'],'worktree','list','--porcelain').decode()
            if 'worktree '+receipt['worktree']+'\n' in listing:
                verify_unstaged(receipt,archive/'worktree')
                # Source directory is absent. No --force and no global prune.
                w.git(receipt['repository'],'worktree','remove',receipt['worktree'])
            journal.update(phase='complete',archive=str(archive),completed_at=w.stamp())
            w.atomic(journal_path,journal)
        w.write_receipt(path,receipt,cleanup='complete',retained=[],disposition=journal)
        return journal
