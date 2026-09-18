"""Exercise review, acceptance and archival with explicit synthetic edits only."""
import hashlib
from pathlib import Path
import tarfile
import time

from . import docker_worker as w


def exercise(root, raw, sudo=False):
    from .worker_evaluation import create_fixture, verify_patch
    began = time.monotonic()
    root.mkdir(mode=0o700)
    repo, revision = create_fixture(root)
    state = root/'attempts'; state.mkdir(mode=0o700)
    receipt = w.run_worker(raw,repo,'HEAD',state,'disposition-fixture',
                           ['sh','fixture.sh','1'],artifact_paths=['result.json'],sudo=sudo)
    if receipt.get('exit_code') != 0 or receipt.get('artifacts') != 'complete':
        raise w.WorkerError('Disposition fixture did not produce complete artifacts')
    tree = Path(receipt['worktree'])
    manifest = Path(receipt['artifact_manifest'])
    patch = (manifest.parent/'changes.patch').read_bytes()
    if not verify_patch(repo,root,receipt,patch,1):
        raise w.WorkerError('Review patch verification failed')
    # Preserve every regular file, including untracked/ignored task outputs.
    # This is a known synthetic fixture, not a general-purpose user-data archiver.
    files = {}
    for path in tree.rglob('*'):
        if path.name == '.git': continue
        if path.is_symlink(): raise w.WorkerError('Synthetic archive unexpectedly contains symlink')
        if path.is_file(): files[str(path.relative_to(tree))] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif not path.is_dir(): raise w.WorkerError('Synthetic archive contains special file')
    archive = root/'reviewed-work.tar'
    with tarfile.open(archive,'w') as output:
        for relative in sorted(files): output.add(tree/relative,arcname=relative,recursive=False)
    with tarfile.open(archive) as source:
        archived = {member.name:hashlib.sha256(source.extractfile(member).read()).hexdigest() for member in source}
    if files != archived: raise w.WorkerError('Archive verification failed')
    # Accept the reviewed source on a separate synthetic worktree; commit makes
    # the selected patch durable without changing the fixture's original checkout.
    accepted = root/'accepted'
    w.git(repo,'worktree','add','--detach',str(accepted),revision)
    w.git(accepted,'apply',str(manifest.parent/'changes.patch'))
    w.git(accepted,'add','library.sh')
    w.git(accepted,'-c','user.name=Synthetic review','-c','user.email=fixture@example.invalid','commit','-qm','Accept reviewed fixture patch')
    accepted_revision = w.git(accepted,'rev-parse','HEAD').decode().strip()
    w.git(repo,'update-ref','refs/heads/reviewed-fixture',accepted_revision)
    w.git(repo,'worktree','remove',str(accepted))
    # Even after the patch was accepted elsewhere, normal recovery retains edits.
    repeated = w.recover(receipt['attempt_path'],sudo=sudo)
    still_retained = repeated['cleanup'] == 'retained' and tree.exists()
    w.verify_worktree(receipt)
    w.git(repo,'worktree','remove','--force',str(tree))
    result = dict(accepted_patch_revision=accepted_revision, patch_verified=True,
                  complete_archive_verified=files==archived, archived_files=len(files),
                  archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
                  runner_retained_after_acceptance=still_retained,
                  manual_cleanup_actions=['archive and verify untracked/ignored outputs','explicitly remove reviewed worktree'],
                  normal_review_actions=['review manifest and patch','apply and commit accepted source'],
                  extra_cleanup_count=2, elapsed_s=time.monotonic()-began,
                  base_checkout_unchanged=not bool(w.git(repo,'status','--porcelain')),
                  remaining_worktrees=w.git(repo,'worktree','list','--porcelain').decode().count('worktree ')-1,
                  scope='Synthetic task through real runner and Git commands; no personal project files')
    w.atomic(root/'disposition.json',result)
    return result
