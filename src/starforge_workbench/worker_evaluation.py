"""Explicit synthetic tmux/Docker trials for the #54 decision gate (no providers)."""
import argparse
import concurrent.futures
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import statistics
import subprocess
import sys
import time
import uuid

from . import docker_worker as w
from .execution import parse_profile, ProfileError

THRESHOLDS = dict(sequential=10, concurrent_pairs=3, failure_repetitions=2,
                  median_launch_overhead_s=5, max_launch_overhead_s=15,
                  runtime_ratio=1.25, short_runtime_allowance_s=2,
                  cold_prepare_s=300, cleanup_s=30, image_budget_bytes=128*1024*1024)
FAILURES = ('invalid_startup', 'cancellation', 'controller_interruption', 'failed_export')
FIXTURE = '''#!/bin/sh
set -eu
version=${1:-1}
read ready ignored < /proc/uptime
mkdir -p .cache
export TMPDIR="$PWD/.cache"
if [ "$version" = 1 ]; then
  printf 'dependency() { echo "$(( $1 + $2 ))"; }\\n' > .cache/dependency.sh
  printf '. ./.cache/dependency.sh\\nanswer() { dependency "$1" "$2"; }\\n' > library.sh
else
  printf 'dependency() { left=${1%%,*}; right=${1#*,}; echo "$(( left + right ))"; }\\n' > .cache/dependency.sh
  printf '. ./.cache/dependency.sh\\nanswer() { dependency "$1,$2"; }\\n' > library.sh
fi
. ./library.sh
test "$(answer 2 3)" = 5
test "$(answer 4 7)" = 11
test "$(answer -2 2)" = 0
read done ignored < /proc/uptime
cg=/sys/fs/cgroup
if [ ! -f "$cg/memory.peak" ]; then
  group=$(sed -n 's/^0:://p' /proc/self/cgroup)
  cg="$cg$group"
fi
peak=$(cat "$cg/memory.peak" 2>/dev/null || printf null)
cpu=$(sed -n 's/^usage_usec //p' "$cg/cpu.stat" 2>/dev/null || true)
[ -n "$cpu" ] || cpu=null
printf '{"assertions":true,"dependency_version":%s,"ready":%s,"done":%s,"memory_peak_bytes":%s,"cpu_usage_usec":%s}\\n' "$version" "$ready" "$done" "$peak" "$cpu" > result.json
'''


def digest(data):
    return hashlib.sha256(data).hexdigest()


def uptime():
    return float(Path('/proc/uptime').read_text().split()[0])


def create_fixture(root):
    repo = root/'fixture'; repo.mkdir()
    w.command(['git', 'init', '-q', str(repo)])
    (repo/'library.sh').write_text('answer() { echo 0; }\n')
    (repo/'fixture.sh').write_text(FIXTURE)
    (repo/'.gitignore').write_text('.cache/\nresult.json\n')
    w.git(repo, 'add', '.')
    w.git(repo, '-c', 'user.name=Synthetic evaluation', '-c', 'user.email=fixture@example.invalid',
          'commit', '-qm', 'Deterministic isolated dependency fixture')
    return repo, w.git(repo, 'rev-parse', 'HEAD').decode().strip()


def profile(image):
    return dict(backend='docker', repository_strategy='per-task-worktree', image=image,
                toolchain='posix-shell-fixture-v1', user=os.getuid(), group=os.getgid(), cpus=1,
                memory_mb=64, pids_limit=32, timeout_seconds=10, network='none',
                mounts=[dict(source='worktree', target='/workspace', read_only=False)])


def trial_plan():
    plan = [('sequential', 1, i) for i in range(THRESHOLDS['sequential'])]
    plan += [('concurrent', version, pair) for pair in range(THRESHOLDS['concurrent_pairs']) for version in (1, 2)]
    plan += [(scenario, 1, i) for scenario in FAILURES for i in range(THRESHOLDS['failure_repetitions'])]
    return plan


def read_json(path):
    return json.loads(path.read_text()) if path.exists() else None


def wait_until(predicate, timeout=20):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value: return value
        time.sleep(.05)
    raise w.WorkerError('Evaluation wait timed out')


def synthetic_cleanup(repo, receipt, sudo):
    """Discard only allocations in this generated fixture after evidence is recorded."""
    path = Path(receipt['worktree'])
    assert Path(receipt['repository']) == repo
    if receipt.get('container_name'):
        item = w.inspect_container(w.docker_prefix(sudo), receipt)
        if item:
            if item['State']['Running']:
                raise w.WorkerError('Refuse evaluation cleanup of running container')
            w.command(w.docker_prefix(sudo)+['rm', item['Id']])
    if path.exists():
        w.verify_worktree(receipt)
        w.git(repo, 'worktree', 'remove', '--force', str(path))


def verify_patch(repo, root, receipt, patch, version):
    path = root/('patch-check-'+uuid.uuid4().hex)
    w.git(repo, 'worktree', 'add', '--detach', str(path), receipt['revision'])
    try:
        patch_file = root/(path.name+'.patch'); patch_file.write_bytes(patch)
        w.git(path, 'apply', '--check', str(patch_file))
        w.git(path, 'apply', str(patch_file))
        # Reproduce dependencies without rewriting the patched library.
        (path/'.cache').mkdir()
        source = Path(receipt['worktree'])/'.cache/dependency.sh'
        (path/'.cache/dependency.sh').write_bytes(source.read_bytes())
        w.command(['sh', '-c', 'cd '+shlex.quote(str(path))+' && . ./library.sh && test "$(answer 2 3)" = 5 && test "$(answer 4 7)" = 11 && test "$(answer -2 2)" = 0'])
        return True
    finally:
        w.git(repo, 'worktree', 'remove', '--force', str(path))


def docker_trial(root, repo, raw, scenario, version, index, sudo):
    identity = uuid.uuid4().hex
    state = root/'docker'; state.mkdir(mode=0o700, exist_ok=True)
    config = root/'profile.json'
    argv = ['sh', 'fixture.sh', str(version)]
    if scenario == 'invalid_startup': argv = ['/missing-evaluation-command']
    if scenario in ('cancellation', 'controller_interruption'): argv = ['sleep', '60']
    if scenario == 'failed_export': argv = ['sh', '-c', 'ln -s /etc/passwd result.json']
    cmd = [sys.executable, '-m', 'starforge_workbench.docker_worker']
    if sudo: cmd.append('--sudo')
    cmd += ['run', '--profile', str(config), '--repository', str(repo), '--revision', 'HEAD',
            '--state-root', str(state), '--task', 'evaluation-'+identity, '--artifact', 'result.json', '--', *argv]
    # Invalid startup and cancelled commands intentionally do not produce a report.
    if scenario in ('invalid_startup', 'cancellation', 'controller_interruption'):
        n = cmd.index('--artifact'); del cmd[n:n+2]
    start = time.monotonic(); start_up = uptime()
    existing = set(state.iterdir())
    with (root/(identity+'.controller.log')).open('wb') as log:
        process = subprocess.Popen(cmd, stdout=log, stderr=log)
        try:
            def find_attempt():
                matches = [p for p in set(state.iterdir())-existing if (p/'worker.json').exists()
                           and read_json(p/'worker.json')['task'] == 'evaluation-'+identity]
                return matches[0] if matches else None
            path = wait_until(find_attempt)
            if scenario in ('cancellation', 'controller_interruption'):
                wait_until(lambda: read_json(path/'worker.json')['phase'] == 'running')
                if scenario == 'cancellation': w.atomic(path/'cancel.request', {'requested_at': w.stamp()})
                else:
                    process.kill(); process.wait(timeout=5)
                    w.recover(path, sudo=sudo, cancel=True)
            process.wait(timeout=35)
        finally:
            if process.poll() is None:
                process.kill(); process.wait(timeout=5)
                if 'path' in locals(): w.recover(path, sudo=sudo, cancel=True)
    receipt = read_json(path/'worker.json')
    elapsed = time.monotonic()-start
    return collect_trial(root, repo, receipt, 'docker', scenario, version, index,
                         elapsed, start_up, sudo)


def native_trial(root, repo, raw, scenario, version, index, sudo):
    identity = uuid.uuid4().hex
    path = root/('native-'+identity); path.mkdir(mode=0o700)
    tree = path/'worktree'; socket = str(path/'tmux.sock')
    unit = 'swb-eval-'+identity+'.scope'
    receipt = dict(attempt_id=identity, repository=str(repo), revision=w.git(repo, 'rev-parse', 'HEAD').decode().strip(),
                   worktree=str(tree), attempt_path=str(path), phase='preparing', cleanup='pending',
                   started_at=w.stamp(), socket=socket, unit=unit, profile={'image': 'native-shell'},
                   artifact_paths=[] if scenario in ('invalid_startup','cancellation','controller_interruption') else ['result.json'])
    w.atomic(path/'worker.json', receipt)
    start = time.monotonic(); start_up = uptime()
    w.git(repo, 'worktree', 'add', '--detach', str(tree), receipt['revision'])
    worker = 'sh fixture.sh '+str(version)
    if scenario == 'invalid_startup': worker = '/missing-evaluation-command'
    if scenario in ('cancellation', 'controller_interruption'): worker = 'sleep 60'
    if scenario == 'failed_export': worker = 'ln -s /etc/passwd result.json'
    # tmux is private to this attempt; the task runs in a bounded transient user scope.
    command = ['systemd-run', '--user', '--scope', '--quiet', '--unit='+unit,
               '-p', 'CPUQuota=100%', '-p', 'MemoryMax=64M', '-p', 'MemorySwapMax=0', '-p', 'TasksMax=32',
               'sh', '-c', 'cd '+shlex.quote(str(tree))+' && '+worker]
    controller = path/'controller.sh'
    controller.write_text('#!/bin/sh\n'+shlex.join(command)+' > '+shlex.quote(str(path/'output.txt'))+
                          ' 2>&1\nrc=$?\nprintf "%s\\n" "$rc" > '+shlex.quote(str(path/'exit.code'))+'\n')
    try:
        w.command(['tmux', '-S', socket, '-f', '/dev/null', 'new-session', '-d', '-s', 'trial', 'sh '+shlex.quote(str(controller))])
        receipt['phase'] = 'running'; w.atomic(path/'worker.json', receipt)
        if scenario in ('cancellation', 'controller_interruption'):
            wait_until(lambda: subprocess.run(['systemctl', '--user', 'is-active', '--quiet', unit]).returncode == 0)
            if scenario == 'controller_interruption':
                # Kill only the private tmux controller. The transient scope is
                # explicitly recovered by its recorded unit identity below.
                w.command(['tmux', '-S', socket, 'kill-server'])
            native_stop(unit, socket)
            receipt.update(phase='cancelled', exit_code=None)
        else:
            wait_until(lambda: (path/'exit.code').exists())
            receipt.update(phase='exited', exit_code=int((path/'exit.code').read_text()))
    except (w.WorkerError, OSError) as exc:
        receipt.update(phase='unknown', reason=str(exc))
    finally:
        native_stop(unit, socket)
    receipt['cleanup'] = 'retained'
    try:
        receipt['artifact_manifest'] = w.export_artifacts([], receipt, None)
        receipt['artifacts'] = 'complete'
        manifest_path = Path(receipt['artifact_manifest'])
        manifest = read_json(manifest_path)
        output = (path/'output.txt').read_bytes()
        if len(output) > w.MAX_ARTIFACT: raise w.WorkerError('Native output exceeds limit')
        (manifest_path.parent/'output.txt').write_bytes(output)
        manifest['files'].append(dict(path='output.txt',bytes=len(output),sha256=digest(output)))
        w.atomic(manifest_path, manifest)
    except (w.WorkerError, OSError) as exc:
        receipt.update(artifacts='incomplete', reason=str(exc))
    w.atomic(path/'worker.json', receipt)
    return collect_trial(root, repo, receipt, 'tmux', scenario, version, index,
                         time.monotonic()-start, start_up, sudo)


def native_stop(unit, socket):
    # Exact private socket and recorded unit. An already absent scope is normal
    # after the tmux controller dies; it is not proof of a command exit status.
    active = subprocess.run(['systemctl', '--user', 'is-active', '--quiet', unit]).returncode
    if active == 0:
        w.command(['systemctl', '--user', 'stop', unit])
    elif active not in (3, 4):
        raise w.WorkerError('Native scope visibility unavailable')
    subprocess.run(['tmux', '-S', socket, 'kill-server'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    if subprocess.run(['systemctl', '--user', 'is-active', '--quiet', unit]).returncode == 0:
        raise w.WorkerError('Native scope still active')


def collect_trial(root, repo, receipt, backend, scenario, version, index, elapsed, start_up, sudo):
    tree = Path(receipt['worktree']); path = Path(receipt['attempt_path'])
    row = dict(backend=backend, attempt_id=receipt['attempt_id'], scenario=scenario, index=index,
               dependency_version=version, elapsed_s=elapsed, phase=receipt['phase'],
               reason=receipt.get('reason'),
               exit_code=receipt.get('exit_code'), artifacts=receipt.get('artifacts'),
               runner_cleanup=receipt['cleanup'], revision=receipt['revision'],
               started_at=receipt.get('started_at') or receipt.get('events',[{}])[0].get('at'),
               observed_end_at=w.stamp(),
               cache='fresh task cache; shared image/OS caches retained',
               recovery_operations=int(scenario == 'controller_interruption'))
    manifest_path = Path(receipt['artifact_manifest']) if receipt.get('artifact_manifest') else path/'artifacts'/'manifest.json'
    if manifest_path.exists():
        row['artifact_manifest_sha256'] = digest(manifest_path.read_bytes())
    row['worktree_bytes'] = sum(p.stat().st_size for p in tree.rglob('*') if p.is_file() and not p.is_symlink()) if tree.exists() else 0
    successful = scenario in ('sequential', 'concurrent')
    if successful:
        result_path = tree/'result.json'
        if result_path.exists() and not result_path.is_symlink():
            row['assertions'] = read_json(result_path)
            row['launch_to_ready_s'] = max(0, row['assertions']['ready']-start_up)
            row['command_s'] = row['assertions']['done']-row['assertions']['ready']
            row['post_command_s'] = max(0, uptime()-row['assertions']['done'])
            patch = w.git(tree, 'diff', '--binary', '--no-ext-diff', '--no-textconv', 'HEAD')
            row['patch_sha256'] = digest(patch)
            row['report_sha256'] = digest(result_path.read_bytes())
            row['patch_applies_and_asserts'] = verify_patch(repo, root, receipt, patch, version)
            if receipt.get('artifact_manifest'):
                manifest = read_json(Path(receipt['artifact_manifest']))
                row['export_hashes_valid'] = all(digest((Path(receipt['artifact_manifest']).parent/f['path']).read_bytes()) == f['sha256'] for f in manifest['files']) and (Path(receipt['artifact_manifest']).parent/'changes.patch').read_bytes() == patch
        else:
            row['error'] = 'Missing fixture report'
    if backend == 'docker':
        recovered = w.recover(path, sudo=sudo)
        row['second_cleanup_harmless'] = recovered['cleanup'] == receipt['cleanup']
    else:
        native_stop(receipt['unit'], receipt['socket'])
        row['second_cleanup_harmless'] = True
    if scenario == 'failed_export':
        row['unsafe_artifact_rejected'] = receipt['artifacts'] == 'incomplete'
        row['preserved'] = tree.exists()
    row['tracked_before_fixture_discard'] = (path/'worker.json').exists()
    # Fixture evidence is already summarized. This is explicit synthetic teardown,
    # not an improvement to the runner's user-facing cleanup behavior.
    row['fixture_discard_required'] = tree.exists()
    synthetic_cleanup(repo, receipt, sudo)
    row['fixture_discard_complete'] = not tree.exists()
    row['remaining_container'] = bool(w.inspect_container(w.docker_prefix(sudo), receipt)) if backend == 'docker' else False
    return row


def boundary_probe(root, repo, raw, sudo):
    state = root/'boundary'; state.mkdir(mode=0o700)
    canary = root/'host-canary'; canary.write_text('synthetic-only')
    script = '''set -eu
    test "$(id -u)" != 0
    test ! -e /var/run/docker.sock
    test ! -e "$1"
    test "$(cat /workspace/.git)" = 'Git administration is host-owned.'
    test "$(awk '/^CapEff:/ {print $2}' /proc/self/status)" = 0000000000000000
    test "$(awk '/^NoNewPrivs:/ {print $2}' /proc/self/status)" = 1
    test "$(cat /sys/fs/cgroup/memory.max)" = 67108864
    test "$(cat /sys/fs/cgroup/pids.max)" = 32
    test "$(cat /sys/fs/cgroup/cpu.max)" = '100000 100000'
    test "$(ls /sys/class/net)" = lo
    ! touch /.synthetic-forbidden
    ! touch /workspace/.git
    echo boundary-ok
    '''
    result = w.run_worker(raw, repo, 'HEAD', state, 'synthetic-boundary', ['sh', '-c', script, 'probe', str(canary)], sudo=sudo)
    boundary_ok = result.get('exit_code') == 0 and result['cleanup'] == 'complete'
    invalid_ok = True
    for field, value in [('network', 'host'), ('user', 0), ('secret_refs', ['unsupported'])]:
        bad = dict(raw); bad[field] = value
        before = set(state.iterdir())
        try: w.run_worker(bad, repo, 'HEAD', state, 'invalid', ['true'], sudo=sudo)
        except (ProfileError, w.WorkerError): pass
        else: invalid_ok = False
        invalid_ok &= set(state.iterdir()) == before
    return dict(runtime_boundary=boundary_ok, invalid_profiles_before_allocation=invalid_ok,
                configuration_verified_before_start=boundary_ok, attempt_id=result['attempt_id'])


def evaluate(rows, boundaries):
    successful = [r for r in rows if r['scenario'] in ('sequential', 'concurrent')]
    docker = [r for r in rows if r['backend'] == 'docker']
    sequential = [r for r in docker if r['scenario'] == 'sequential']
    warm = {b: [r for r in successful if r['backend'] == b and r['scenario'] == 'sequential' and r['index'] > 0] for b in ('docker','tmux')}
    safety = all(boundaries.get(k) is True for k in ('runtime_boundary','invalid_profiles_before_allocation','configuration_verified_before_start'))
    reproducible = len(sequential) == 10 and all(r.get('assertions', {}).get('assertions') and r.get('patch_applies_and_asserts') for r in sequential) and len({r.get('patch_sha256') for r in sequential}) == 1
    metrics = {}
    if all(len(warm[b]) == 9 and all('command_s' in r for r in warm[b]) for b in warm):
        overhead = [d['launch_to_ready_s']-n['launch_to_ready_s'] for d,n in zip(warm['docker'],warm['tmux'])]
        metrics = dict(median_launch_overhead_s=statistics.median(overhead), max_launch_overhead_s=max(overhead),
                       docker_median_command_s=statistics.median(r['command_s'] for r in warm['docker']),
                       native_median_command_s=statistics.median(r['command_s'] for r in warm['tmux']))
    counts = Counter((r['backend'],r['scenario'],r['dependency_version'],r['index']) for r in rows)
    expected = Counter((b,*spec) for b in ('docker','tmux') for spec in trial_plan())
    complete = counts == expected
    runtime_pass = bool(metrics) and metrics['median_launch_overhead_s'] <= THRESHOLDS['median_launch_overhead_s'] and metrics['max_launch_overhead_s'] <= THRESHOLDS['max_launch_overhead_s'] and metrics['docker_median_command_s'] <= THRESHOLDS['runtime_ratio']*metrics['native_median_command_s']+THRESHOLDS['short_runtime_allowance_s']
    pairs = [r for r in docker if r['scenario'] == 'concurrent']
    isolation = len(pairs) == 6 and len({r.get('attempt_id') for r in pairs}) == 6 and all(r.get('assertions',{}).get('dependency_version') == r['dependency_version'] for r in pairs)
    cleanup = complete and all(r.get('tracked_before_fixture_discard') and r.get('fixture_discard_complete') and r.get('second_cleanup_harmless') and not r.get('remaining_container') and r.get('elapsed_s',float('inf')) <= 40 for r in rows)
    artifacts = len(successful) == 32 and all(r.get('patch_applies_and_asserts') and r.get('export_hashes_valid') for r in successful)
    artifacts &= all(r.get('unsafe_artifact_rejected') and r.get('preserved') for r in rows if r['scenario'] == 'failed_export')
    # A shell fixture can show correctness/isolation, but not the ADR's unique
    # benefit of incompatible pinned toolchains. Never promote that missing proof.
    decision = 'stop' if not safety else 'iterate on the basic runner'
    return dict(recommendation=decision, mandatory_boundaries='pass' if safety else 'fail',
                reproducibility='pass' if reproducible else 'fail', sample_complete=complete,
                runtime_measurements=metrics, timing_thresholds='pass' if runtime_pass else 'fail',
                shell_dependency_isolation='pass' if isolation else 'fail',
                setup_cleanup='pass' if cleanup else 'fail', artifact_usefulness='pass' if artifacts else 'fail',
                cold_preparation='partial: task caches are empty; image is pre-cached',
                toolchain_comparability='unknown',
                concrete_docker_benefit='not demonstrated',
                operator_friction='unknown: synthetic teardown is automated; production edit disposition was not exercised',
                remaining_evidence=['Same pinned toolchain for native and Docker; current shells differ.',
                                    'A real incompatible toolchain dependency comparison; shell API fixtures alone are insufficient.',
                                    'Ordinary cleanup friction is unproven: the harness discards only its synthetic edits; real edits need review/disposition.'],
                conditional_expansion_authorized=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True, help='New private evaluation directory')
    parser.add_argument('--image', required=True, help='Already cached shell image pinned by digest')
    parser.add_argument('--sudo', action='store_true')
    args = parser.parse_args()
    raw = profile(args.image); parse_profile(raw)
    args.root.mkdir(mode=0o700)
    root = w.private_directory(args.root)
    repo, revision = create_fixture(root)
    w.atomic(root/'profile.json', raw)
    image = json.loads(w.command(w.docker_prefix(args.sudo)+['image','inspect',args.image]))[0]
    result = dict(version=1, thresholds=THRESHOLDS, profile=raw, fixture_revision=revision,
                  image_size_bytes=image['Size'], image_download='preexisting cache; not measured in this run',
                  network={'docker':'none','tmux':'host; fixture makes no network requests'},
                  toolchains={'docker':args.image, 'tmux':{'executable':Path(shutil.which('sh')).resolve().name, 'sha256':digest(Path(shutil.which('sh')).resolve().read_bytes())}},
                  host_load_start=list(os.getloadavg()), started_at=w.stamp(), trials=[])
    w.atomic(root/'results.json', result)  # Declare thresholds before observations.
    result['boundaries'] = boundary_probe(root, repo, raw, args.sudo)
    if all(result['boundaries'][k] for k in ('runtime_boundary','invalid_profiles_before_allocation','configuration_verified_before_start')):
        for backend, runner in [('tmux',native_trial),('docker',docker_trial)]:
            for scenario, version, index in trial_plan():
                if scenario == 'concurrent' and version == 2: continue
                specs = [(scenario,version,index)] if scenario != 'concurrent' else [(scenario,v,index) for v in (1,2)]
                with concurrent.futures.ThreadPoolExecutor(max_workers=len(specs)) as pool:
                    futures = [pool.submit(runner,root,repo,raw,*spec,args.sudo) for spec in specs]
                    for future, spec in zip(futures,specs):
                        try: row = future.result()
                        except Exception as exc:
                            row = dict(backend=backend,scenario=spec[0],dependency_version=spec[1],index=spec[2],error=type(exc).__name__+': '+str(exc))
                        result['trials'].append(row)
                        w.atomic(root/'results.json',result)
                print(backend,scenario,index,flush=True)
    result['base_checkout_unchanged'] = not w.git(repo,'status','--porcelain','--ignored')
    result['remaining_worktrees'] = w.git(repo,'worktree','list','--porcelain').decode().count('worktree ')-1
    result['decision'] = evaluate(result['trials'],result['boundaries'])
    result['decision']['base_checkout_unchanged'] = result['base_checkout_unchanged']
    result['decision']['image_disk_budget'] = 'pass' if image['Size'] <= THRESHOLDS['image_budget_bytes'] else 'fail'
    result['finished_at'] = w.stamp(); result['host_load_end'] = list(os.getloadavg())
    w.atomic(root/'results.json',result)
    report = '# Docker worker evaluation\n\nRecommendation: **'+result['decision']['recommendation']+'**.\n\n'
    report += f"Recorded {len(result['trials'])} trials. Boundaries: {result['decision']['mandatory_boundaries']}. Reproducibility: {result['decision']['reproducibility']}.\n\n"
    report += 'Measured warm launch overhead: '+str(result['decision']['runtime_measurements'])+'\n\n'
    report += 'Full rubric: '+json.dumps(result['decision'],sort_keys=True)+'\n\n'
    report += '\n'.join('- '+reason for reason in result['decision']['remaining_evidence'])+'\n\n#51 and #53 remain gated. No provider or daily launcher changes.\n'
    (root/'report.md').write_text(report)
    print(report)


if __name__ == '__main__':
    main()
