from collections import Counter
from starforge_workbench import worker_evaluation as e


def test_declared_sample_size():
    plan = e.trial_plan()
    assert len(plan) == 24
    assert Counter(p[0] for p in plan) == dict(sequential=10, concurrent=6, invalid_startup=2,
                                             cancellation=2, controller_interruption=2, failed_export=2)
    assert len(set(plan)) == len(plan)


def test_no_evidence_is_never_a_pass():
    decision = e.evaluate([], {})
    assert decision['recommendation'] == 'stop'
    assert decision['mandatory_boundaries'] == 'fail'
    assert decision['reproducibility'] == 'fail'
    assert decision['sample_complete'] is False
    assert decision['conditional_expansion_authorized'] is False


def test_boundary_pass_does_not_authorize_expansion():
    decision = e.evaluate([], dict(runtime_boundary=True, invalid_profiles_before_allocation=True,
                                   configuration_verified_before_start=True))
    assert decision['recommendation'] == 'iterate on the basic runner'
    assert decision['toolchain_comparability'] == 'unknown'
    assert decision['timing_thresholds'] == 'fail'
    assert decision['artifact_usefulness'] == 'fail'


def test_duplicate_trials_do_not_satisfy_sample_gate():
    row = dict(backend='docker', scenario='invalid_startup', dependency_version=1, index=0)
    assert e.evaluate([row]*48, {})['sample_complete'] is False


def test_fixture_is_deterministic_and_patches_reapply(tmp_path):
    root = tmp_path/'evaluation'; root.mkdir(mode=0o700)
    repo, revision = e.create_fixture(root)
    for version in (1,2):
        tree = root/f'trial-{version}'
        e.w.git(repo,'worktree','add','--detach',str(tree),revision)
        e.w.command(['sh','-c',f'cd {tree} && sh fixture.sh {version}'])
        result = e.read_json(tree/'result.json')
        assert result['assertions'] and result['dependency_version'] == version
        patch = e.w.git(tree,'diff','--binary','HEAD')
        receipt = dict(worktree=str(tree),revision=revision)
        assert e.verify_patch(repo,root,receipt,patch,version)
    assert not e.w.git(repo,'status','--porcelain')


def test_failed_trial_is_kept_and_fails_reproducibility():
    import json
    from pathlib import Path
    evidence = Path(__file__).resolve().parents[1]/'docs/evaluations/docker-workers-54.json'
    result = json.loads(evidence.read_text())
    decision = e.evaluate(result['trials'], result['boundaries'])
    assert decision['sample_complete'] and decision['reproducibility'] == 'pass'
    trial = next(t for t in result['trials'] if t['backend'] == 'docker' and t['scenario'] == 'sequential')
    trial['patch_applies_and_asserts'] = False
    decision = e.evaluate(result['trials'], result['boundaries'])
    assert decision['reproducibility'] == 'fail'
    assert decision['artifact_usefulness'] == 'fail'
    assert decision['conditional_expansion_authorized'] is False


def test_cleanup_declared_budget_includes_slow_teardown():
    assert e.cleanup_within_budget({'cleanup_upper_bound_s':30})
    assert not e.cleanup_within_budget({'cleanup_upper_bound_s':31, 'controller_elapsed_s':1})
    for value in (None, -1, float('inf'), float('nan'), True):
        assert not e.cleanup_within_budget({'cleanup_upper_bound_s':value})
    assert not e.cleanup_within_budget({'elapsed_s':1})  # historical measurements cannot pass


def test_disk_budget_counts_download_and_native_extraction():
    mb=1024*1024
    assert e.disk_usage(100*mb,dict(stored_bytes=20*mb,extracted_toolchain_bytes=9*mb))['decision']=='fail'
    assert e.disk_usage(100*mb,dict(stored_bytes=20*mb,extracted_toolchain_bytes=8*mb))['decision']=='pass'
    assert e.disk_usage(1,dict(stored_bytes=1))['decision']=='unknown'


def test_review_waits_for_peer_finalizer_even_on_failure(monkeypatch):
    import threading
    entered=threading.Event();finished=threading.Event()
    def runner(root,repo,raw,scenario,version,index,sudo,defer):
        assert defer
        if version==1:
            entered.set()
            return ('fast',)
        assert entered.wait(2)
        finished.set()
        raise RuntimeError('synthetic finalizer failure')
    def collect(*args):
        raise AssertionError('Review must be deferred after peer failure')
    monkeypatch.setattr(e,'collect_trial',collect)
    result=e.run_batch(runner,None,None,None,[('concurrent',1,0),('concurrent',2,0)],False)
    assert 'Peer finalizer failed' in result[0]['error']
    assert 'synthetic finalizer failure' in result[1]['error']


def test_collection_timing_includes_recovery_and_discard(tmp_path,monkeypatch):
    import json
    path=tmp_path/'attempt';path.mkdir()
    (path/'worker.json').write_text('{}')
    receipt=dict(attempt_path=str(path),worktree=str(path/'gone'),attempt_id='fixture',
                 phase='exited',cleanup='complete',revision='revision',unit='unit',socket='socket')
    clock=[0.0]
    monkeypatch.setattr(e,'uptime',lambda:clock[0])
    def recover(*args):clock[0]+=5
    def discard(*args):clock[0]+=26
    monkeypatch.setattr(e,'native_stop',recover)
    monkeypatch.setattr(e,'synthetic_cleanup',discard)
    row=e.collect_trial(tmp_path,tmp_path,receipt,'tmux','invalid_startup',1,0,1,0,False)
    assert row['controller_elapsed_s']==1
    assert row['cleanup_upper_bound_s'] > 31
    assert not e.cleanup_within_budget(row)


def test_fast_peer_cannot_start_review_while_slow_peer_is_finalizing(monkeypatch):
    import concurrent.futures
    import threading
    entered=threading.Event();release=threading.Event();reviewed=threading.Event()
    def runner(root,repo,raw,scenario,version,index,sudo,defer):
        if version==2:
            entered.set()
            assert release.wait(3)
        return (version,)
    def collect(version):
        reviewed.set()
        return {'version':version}
    monkeypatch.setattr(e,'collect_trial',collect)
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        future=pool.submit(e.run_batch,runner,None,None,None,[('concurrent',1,0),('concurrent',2,0)],False)
        try:
            assert entered.wait(2)
            assert not reviewed.wait(.1)
        finally:
            release.set()
        assert len(future.result(timeout=3))==2
