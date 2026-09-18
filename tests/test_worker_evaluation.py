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
