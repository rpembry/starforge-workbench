import json
from pathlib import Path
import pytest
from starforge_workbench import docker_worker as w
from starforge_workbench import worker_disposition as d


@pytest.fixture
def attempt(tmp_path,monkeypatch):
    repo=tmp_path/'repo';repo.mkdir()
    w.command(['git','init','-q',str(repo)])
    (repo/'source').write_text('base')
    (repo/'.gitignore').write_text('ignored\n')
    w.git(repo,'add','.')
    w.git(repo,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','fixture')
    path=tmp_path/('a'*32);path.mkdir(mode=0o700)
    w.git(repo,'worktree','add','--detach',str(path/'worktree'),'HEAD')
    (path/'worktree/source').write_text('changed')
    (path/'worktree/ignored').write_text('keep ignored')
    (path/'worktree/new').write_text('keep untracked')
    (path/'worktree/empty').mkdir()
    (path/'scratch').mkdir();(path/'scratch/output').write_bytes(b'\x00data')
    (path/'artifacts').mkdir();(path/'artifacts/result').write_text('export')
    receipt=dict(attempt_id=path.name,attempt_path=str(path),worktree=str(path/'worktree'),
                 container_name='swb-'+path.name,token='owned',repository=str(repo),
                 revision=w.git(repo,'rev-parse','HEAD').decode().strip(),phase='exited',artifacts='complete',cleanup='retained')
    w.atomic(path/'worker.json',receipt)
    monkeypatch.setattr(w,'inspect_container',lambda *args:None)
    return path,repo


def test_complete_archive_and_idempotent_disposition(attempt):
    path,repo=attempt
    approved=d.review(path)
    before=d.inventory({name:path/name for name in d.ROOTS})
    result=d.dispose(path,approved['review_sha256'])
    assert result['phase']=='complete'
    assert not (path/'worktree').exists() and not (path/'scratch').exists()
    assert d.inventory({name:(path/name if name=='artifacts' else path/'archive'/name) for name in d.ROOTS})==before
    assert d.dispose(path,approved['review_sha256'])==result
    assert w.git(repo,'worktree','list','--porcelain').decode().count('worktree ')==1
    assert (repo/'source').read_text()=='base'


@pytest.mark.parametrize('name',['source','ignored','new'])
def test_changes_after_review_are_preserved(attempt,name):
    path,_=attempt;approved=d.review(path)
    (path/'worktree'/name).write_text('later edit')
    with pytest.raises(w.WorkerError,match='changed since review'):
        d.dispose(path,approved['review_sha256'])
    assert (path/'worktree'/name).read_text()=='later edit'
    assert not (path/'archive').exists()


def test_wrong_token_refuses_mutation(attempt):
    path,_=attempt;d.review(path)
    with pytest.raises(w.WorkerError,match='token'):
        d.dispose(path,'wrong')
    assert (path/'worktree').exists()


def test_symlink_refused_without_following(attempt):
    path,_=attempt;(path/'worktree/link').symlink_to('/etc/passwd')
    with pytest.raises(w.WorkerError,match='symlinks'):
        d.review(path)
    assert not (path/'review.json').exists()


def test_live_or_unknown_container_refuses_disposition(attempt,monkeypatch):
    path,_=attempt;approved=d.review(path)
    monkeypatch.setattr(w,'inspect_container',lambda *args:{'State':{'Running':True}})
    with pytest.raises(w.WorkerError,match='container'):
        d.dispose(path,approved['review_sha256'])
    assert (path/'worktree').exists()


def test_retry_after_archive_before_unregister(attempt,monkeypatch):
    path,_=attempt;approved=d.review(path);original=w.git
    def interrupt(repo,*args):
        if args[:2]==('worktree','remove'):raise w.WorkerError('injected interruption')
        return original(repo,*args)
    monkeypatch.setattr(w,'git',interrupt)
    with pytest.raises(w.WorkerError,match='interruption'):
        d.dispose(path,approved['review_sha256'])
    assert (path/'archive/worktree/new').read_text()=='keep untracked'
    monkeypatch.setattr(w,'git',original)
    assert d.dispose(path,approved['review_sha256'])['phase']=='complete'


def test_tampered_archive_refuses_retry(attempt,monkeypatch):
    path,_=attempt;approved=d.review(path);original=w.git
    def interrupt(repo,*args):
        if args[:2]==('worktree','remove'):raise w.WorkerError('injected interruption')
        return original(repo,*args)
    monkeypatch.setattr(w,'git',interrupt)
    with pytest.raises(w.WorkerError):d.dispose(path,approved['review_sha256'])
    monkeypatch.setattr(w,'git',original)
    (path/'archive/worktree/new').write_text('later archive edit')
    with pytest.raises(w.WorkerError,match='content changed'):
        d.dispose(path,approved['review_sha256'])
    assert (path/'archive/worktree/new').read_text()=='later archive edit'


def test_staged_work_is_not_lost(attempt):
    path,_=attempt
    w.git(path/'worktree','add','source')
    with pytest.raises(w.WorkerError,match='Staged changes'):
        d.review(path)
    assert (path/'worktree').exists()


def test_staging_after_review_refuses_disposal(attempt):
    path,_=attempt;approved=d.review(path)
    w.git(path/'worktree','add','source')
    with pytest.raises(w.WorkerError,match='Staged changes'):
        d.dispose(path,approved['review_sha256'])
    assert (path/'worktree').exists()


def test_hardlinks_are_refused(attempt):
    import os
    path,_=attempt
    os.link(path/'worktree/source',path/'worktree/alias')
    with pytest.raises(w.WorkerError,match='hardlinks'):
        d.review(path)


def test_scratch_change_invalidates_approval(attempt):
    path,_=attempt;approved=d.review(path)
    (path/'scratch/output').write_bytes(b'new output')
    with pytest.raises(w.WorkerError,match='changed since review'):
        d.dispose(path,approved['review_sha256'])
    assert (path/'scratch/output').read_bytes()==b'new output'


def test_reappearing_source_after_archive_is_not_removed(attempt,monkeypatch):
    path,_=attempt;approved=d.review(path);original=d.fsync_tree
    def replace_source(root):
        original(root)
        source=path/'worktree'
        source.mkdir(exist_ok=True)
        (source/'new-host-edit').write_text('preserve')
    monkeypatch.setattr(d,'fsync_tree',replace_source)
    with pytest.raises(w.WorkerError,match='reappeared'):
        d.dispose(path,approved['review_sha256'])
    assert (path/'worktree/new-host-edit').read_text()=='preserve'
    assert (path/'archive/worktree/new').read_text()=='keep untracked'


def test_retry_after_unregister_before_completion_receipt(attempt,monkeypatch):
    path,repo=attempt;approved=d.review(path);original=w.atomic
    def interrupt(target,value):
        if target.name=='disposition.json' and value.get('phase')=='complete':
            raise OSError('injected journal interruption')
        return original(target,value)
    monkeypatch.setattr(w,'atomic',interrupt)
    with pytest.raises(OSError,match='journal interruption'):
        d.dispose(path,approved['review_sha256'])
    assert w.git(repo,'worktree','list','--porcelain').decode().count('worktree ')==1
    monkeypatch.setattr(w,'atomic',original)
    assert d.dispose(path,approved['review_sha256'])['phase']=='complete'
    assert (path/'archive/worktree/new').read_text()=='keep untracked'
