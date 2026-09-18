"""Supplemental #54 capability comparison: legacy and modern Python stdlib tools."""
import argparse
import concurrent.futures
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import time
import uuid

from . import docker_worker as w
from .worker_evaluation import native_stop, wait_until, THRESHOLDS

CASES = {
    'legacy': '''from distutils.version import StrictVersion
assert StrictVersion("1.9") < StrictVersion("1.10")
assert str(StrictVersion("2.0.1")) == "2.0.1"
''',
    'modern': '''from typing import ReadOnly, TypedDict
from annotationlib import get_annotations, Format
class Release(TypedDict):
    version: ReadOnly[str]
assert Release.__readonly_keys__ == frozenset({"version"})
assert "version" in get_annotations(Release, format=Format.STRING)
''',
}
REPORT = '''
import json,sys,platform
print(json.dumps({"assertions":True,"version":sys.version.split()[0],"libc":platform.libc_ver()[0]}))
'''


def expected_missing_api(case, error):
    if case == 'legacy': return "ModuleNotFoundError: No module named 'distutils'" in error
    return "ImportError: cannot import name 'ReadOnly'" in error


def fixture(root):
    repo=root/'fixture';repo.mkdir()
    w.command(['git','init','-q',str(repo)])
    for name,body in CASES.items(): (repo/(name+'.py')).write_text(body+REPORT)
    w.git(repo,'add','.')
    w.git(repo,'-c','user.name=Synthetic evaluation','-c','user.email=fixture@example.invalid','commit','-qm','Python capability fixtures')
    return repo


def profile(image):
    return dict(backend='docker',repository_strategy='per-task-worktree',image=image,
                toolchain='python-stdlib-capability',user=os.getuid(),group=os.getgid(),
                cpus=1,memory_mb=64,pids_limit=32,timeout_seconds=10,network='none',
                mounts=[dict(source='worktree',target='/workspace',read_only=False)])


def native(root,repo,executable,case):
    identity=uuid.uuid4().hex
    attempt=root/('n-'+identity);attempt.mkdir(mode=0o700)
    tree=attempt/'tree';socket=str(attempt/'tmux.sock');unit='swb-python-'+identity+'.scope'
    w.atomic(attempt/'ownership.json',dict(repository=str(repo),worktree=str(tree),socket=socket,unit=unit))
    w.git(repo,'worktree','add','--detach',str(tree),'HEAD')
    # -I -S excludes user environment and compatibility packages from both
    # backends. This compares actual stdlib contracts, not installed shims.
    command=['systemd-run','--user','--scope','--quiet','--unit='+unit,
             '-p','CPUQuota=100%','-p','MemoryMax=64M','-p','MemorySwapMax=0','-p','TasksMax=32',
             executable,'-I','-S','-B',str(tree/(case+'.py'))]
    wrapper=attempt/'controller.sh'
    wrapper.write_text('#!/bin/sh\n'+shlex.join(command)+' > '+shlex.quote(str(attempt/'output'))+
                       ' 2> '+shlex.quote(str(attempt/'error'))+'\nrc=$?\nprintf "%s\\n" "$rc" > '+shlex.quote(str(attempt/'exit'))+'\n')
    start=time.monotonic()
    try:
        w.command(['tmux','-S',socket,'-f','/dev/null','new-session','-d','-s','fixture','sh '+shlex.quote(str(wrapper))])
        wait_until(lambda:(attempt/'exit').exists(),timeout=10)
        code=int((attempt/'exit').read_text())
        output=(attempt/'output').read_text()
        result=dict(backend='tmux',attempt_id=identity,case=case,exit_code=code,
                    assertions=json.loads(output) if code==0 else None,
                    elapsed_s=time.monotonic()-start,expected_api_absent=expected_missing_api(case,(attempt/'error').read_text()))
    finally:
        native_stop(unit,socket)
    # Neither case writes source or caches. Refuse unexpected dirty teardown.
    if w.git(tree,'status','--porcelain','--ignored'):
        raise w.WorkerError('Unexpected native fixture changes; preserved for review')
    w.git(repo,'worktree','remove',str(tree))
    result['cleanup']='complete'
    w.atomic(attempt/'result.json',result)
    return result


def docker(root,repo,image,case,sudo):
    state=root/'docker';state.mkdir(mode=0o700,exist_ok=True)
    start=time.monotonic()
    receipt=w.run_worker(profile(image),repo,'HEAD',state,'python-capability-'+case,
                         ['python','-I','-S','-B',case+'.py'],sudo=sudo)
    output=Path(receipt.get('artifact_manifest','')).parent/'output.txt'
    # Warnings may share the combined output; parse the single JSON result line.
    reports=[json.loads(line) for line in output.read_text().splitlines() if line.startswith('{')] if output.exists() else []
    return dict(backend='docker',attempt_id=receipt['attempt_id'],case=case,
                exit_code=receipt.get('exit_code'),assertions=reports[-1] if reports else None,
                elapsed_s=time.monotonic()-start,cleanup=receipt['cleanup'],image=image,
                artifacts_complete=receipt.get('artifacts')=='complete',
                expected_api_absent=expected_missing_api(case,output.read_text() if output.exists() else ''))


def decide(rows, disk_bytes, base_unchanged, native_unchanged):
    expected=[r for r in rows if not r.get('cross_check')]
    negative=[r for r in rows if r.get('cross_check')]
    counts=Counter((r['backend'],r['case'],r['pair']) for r in expected)
    complete=counts==Counter((b,c,i) for b in ('tmux','docker') for c in CASES for i in range(3))
    complete &= Counter((r['backend'],r['case']) for r in negative)==Counter((b,c) for b in ('tmux','docker') for c in CASES)
    success=complete and all(r.get('exit_code')==0 and (r.get('assertions') or {}).get('assertions') and r.get('cleanup')=='complete' for r in expected)
    conflicts=complete and all(r.get('exit_code') not in (None,0) and r.get('expected_api_absent') is True and r.get('cleanup')=='complete' for r in negative)
    within_disk=disk_bytes <= THRESHOLDS['image_budget_bytes']
    healthy=success and conflicts and base_unchanged and native_unchanged and within_disk
    return dict(recommendation='stop expansion for this workload' if healthy else 'iterate: incomplete or failed evidence',
                sample_complete=complete,concurrent_capabilities_pass=success,
                incompatible_stdlib_contracts_verified=conflicts,
                image_disk_budget='pass' if within_disk else 'fail',
                native_already_satisfies_workload=healthy,
                concrete_incremental_docker_benefit=False,
                conditional_expansion_authorized=False,
                rationale='Both existing native runtimes already run these incompatible stdlib contracts concurrently with no install or default change; no observed native repair/setup step is avoided by Docker.' if healthy else 'Do not infer benefit from incomplete evidence.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    for case in CASES:
        parser.add_argument('--'+case+'-image',required=True)
        parser.add_argument('--'+case+'-python',required=True)
    parser.add_argument('--sudo',action='store_true')
    args=parser.parse_args()
    images={case:getattr(args,case+'_image') for case in CASES}
    executables={case:str(Path(getattr(args,case+'_python')).resolve(strict=True)) for case in CASES}
    for image in images.values(): w.parse_profile(profile(image))
    args.root.mkdir(mode=0o700)
    root=w.private_directory(args.root);repo=fixture(root)
    fingerprint=lambda:{case:hashlib.sha256(Path(path).read_bytes()).hexdigest() for case,path in executables.items()}
    before=fingerprint()
    default_python=Path('/usr/bin/python3').resolve()
    result=dict(plan={'pairs_per_backend':3,'cross_checks_per_backend':2,
                      'limits':{'cpus':1,'memory_mb':64,'pids_limit':32,'timeout_seconds':10},
                      'disk_budget_bytes':THRESHOLDS['image_budget_bytes'],
                      'network':'Docker none; native host network but fixtures perform no network operations',
                      'python_flags':['-I','-S','-B'],'no_dependency_installs':True},
                fixture_revision=w.git(repo,'rev-parse','HEAD').decode().strip(),
                native_binary_hashes=before,images=images,preparation={},trials=[],started_at=w.stamp())
    w.atomic(root/'results.json',result)
    prefix=w.docker_prefix(args.sudo)
    # Explicit preparation only; worker launch itself retains --pull never.
    for case,image in images.items():
        try:
            w.command(prefix+['image','inspect',image])
            cached=True
        except w.WorkerError:
            cached=False
        began=time.monotonic()
        w.command(prefix+['pull',image],timeout=300)
        metadata=json.loads(w.command(prefix+['image','inspect',image]))[0]
        result['preparation'][case]={'cached_before_pull':cached,'pull_s':time.monotonic()-began,'cached_size_bytes':metadata['Size']}
        w.atomic(root/'results.json',result)
    total=sum(x['cached_size_bytes'] for x in result['preparation'].values())
    result['runtime_storage_bytes']=total
    if total <= THRESHOLDS['image_budget_bytes']:
        for backend,runner in [('tmux',native),('docker',docker)]:
            for pair in range(3):
                with concurrent.futures.ThreadPoolExecutor(2) as pool:
                    futures={case:pool.submit(runner,root,repo,executables[case] if backend=='tmux' else images[case],case,*([args.sudo] if backend=='docker' else [])) for case in CASES}
                    concurrent.futures.wait(futures.values())
                    for case,future in futures.items():
                        try: row=future.result()
                        except Exception as exc: row=dict(backend=backend,case=case,error=type(exc).__name__+': '+str(exc))
                        row.update(pair=pair,cross_check=False)
                        result['trials'].append(row)
                w.atomic(root/'results.json',result)
                print(backend,'pair',pair,flush=True)
            for case,other in [('legacy','modern'),('modern','legacy')]:
                try: row=runner(root,repo,executables[other] if backend=='tmux' else images[other],case,*([args.sudo] if backend=='docker' else []))
                except Exception as exc: row=dict(backend=backend,case=case,error=type(exc).__name__+': '+str(exc))
                row['cross_check']=True;result['trials'].append(row)
                w.atomic(root/'results.json',result)
    unchanged=not w.git(repo,'status','--porcelain','--ignored')
    native_same=before==fingerprint() and Path('/usr/bin/python3').resolve()==default_python
    result.update(base_checkout_unchanged=unchanged,native_toolchain_unchanged=native_same,
                  remaining_worktrees=w.git(repo,'worktree','list','--porcelain').decode().count('worktree ')-1,
                  finished_at=w.stamp())
    result['decision']=decide(result['trials'],total,unchanged,native_same)
    if result['remaining_worktrees']:
        result['decision'].update(recommendation='iterate: retained resources require investigation',native_already_satisfies_workload=False)
    w.atomic(root/'results.json',result)
    print(json.dumps(result['decision'],indent=2))


if __name__=='__main__':main()
