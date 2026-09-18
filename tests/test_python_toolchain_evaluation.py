from starforge_workbench import python_toolchain_evaluation as e


def rows():
    result=[]
    for backend in ('tmux','docker'):
        for case in e.CASES:
            for pair in range(3):
                result.append(dict(backend=backend,case=case,pair=pair,cross_check=False,
                                   exit_code=0,assertions={'assertions':True},cleanup='complete'))
            result.append(dict(backend=backend,case=case,cross_check=True,exit_code=1,
                               expected_api_absent=True,cleanup='complete'))
    return result


def test_success_on_both_backends_does_not_manufacture_docker_benefit():
    result=e.decide(rows(),1024,True,True)
    assert result['sample_complete'] and result['concurrent_capabilities_pass']
    assert result['native_already_satisfies_workload']
    assert result['recommendation']=='stop expansion for this workload'
    assert not result['concrete_incremental_docker_benefit']
    assert not result['conditional_expansion_authorized']


def test_missing_and_duplicate_samples_cannot_complete():
    data=rows();data[0]=dict(data[1])
    assert not e.decide(data,1024,True,True)['sample_complete']
    assert not e.decide([],1024,True,True)['sample_complete']


def test_unrelated_failure_is_not_a_toolchain_conflict():
    data=rows();next(r for r in data if r['cross_check'])['expected_api_absent']=False
    assert not e.decide(data,1024,True,True)['incompatible_stdlib_contracts_verified']
    assert not e.expected_missing_api('legacy','No module named unrelated')
    assert e.expected_missing_api('legacy',"ModuleNotFoundError: No module named 'distutils'")
    assert e.expected_missing_api('modern',"ImportError: cannot import name 'ReadOnly' from 'typing'")


def test_budget_and_host_mutation_block_favorable_results():
    assert not e.decide(rows(),e.THRESHOLDS['image_budget_bytes']+1,True,True)['native_already_satisfies_workload']
    assert not e.decide(rows(),1024,False,True)['native_already_satisfies_workload']
    assert not e.decide(rows(),1024,True,False)['native_already_satisfies_workload']


def test_zero_exit_without_assertion_report_is_not_success():
    data=rows();data[0]['assertions']=None
    result=e.decide(data,1024,True,True)
    assert not result['concurrent_capabilities_pass']
    assert result['recommendation'].startswith('iterate:')
