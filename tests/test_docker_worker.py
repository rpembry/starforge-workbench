"""Synthetic repositories; live Docker checks require explicit opt-in and a cached image."""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from starforge_workbench import docker_worker as w


def profile():
    return dict(backend='docker', repository_strategy='per-task-worktree',
                image=os.environ.get('WB_TEST_DOCKER_IMAGE', 'example.invalid/test@sha256:'+'a'*64),
                toolchain='test-shell', user=os.getuid(), group=os.getgid(), cpus=1,
                memory_mb=64, pids_limit=32, timeout_seconds=15, network='none',
                mounts=[dict(source='worktree', target='/workspace', read_only=False)])


@pytest.fixture
def repo(tmp_path):
    path = tmp_path/'repo'; path.mkdir()
    subprocess.run(['git', 'init', '-q', str(path)], check=True)
    (path/'hello.txt').write_text('original\n')
    for args in [('add', 'hello.txt'), ('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fixture')]:
        subprocess.run(['git', '-C', str(path), *args], check=True)
    return path


@pytest.fixture
def state(tmp_path):
    path = tmp_path/'attempts'; path.mkdir(mode=0o700)
    return path


def test_command_bounds_and_stderr():
    assert w.command([sys.executable, '-c', 'import sys;print("error",file=sys.stderr)'], combined=True) == b'error\n'
    with pytest.raises(w.WorkerError, match='limit'):
        w.command([sys.executable, '-c', 'print("x"*10000)'], limit=100)
    with pytest.raises(w.WorkerError, match='timed out'):
        w.command([sys.executable, '-c', 'import time;time.sleep(10)'], timeout=.05)


@pytest.mark.parametrize('name', ['../secret', '/etc/passwd', '.git', 'x/.git/config', 'a\x00b'])
def test_reject_artifact_escape_before_runtime(name, repo, state):
    with pytest.raises(w.WorkerError, match='Artifact paths'):
        w.run_worker(profile(), repo, 'HEAD', state, 'synthetic', ['true'], [name])
    assert not list(state.iterdir())


def test_security_arguments():
    p = w.parse_profile(profile())
    r = dict(attempt_path='/tmp/attempt', attempt_id='a'*32, token='token',
             container_name='swb-test', argv=['sh', '-c', 'true'])
    argv = w.create_argv(['docker'], r, p)
    for flag, value in [('--pull', 'never'), ('--network', 'none'), ('--cap-drop', 'ALL'), ('--security-opt', 'no-new-privileges'),
                        ('--restart', 'no'), ('--memory-swap', '64m'), ('--user', f'{os.getuid()}:{os.getgid()}')]:
        assert argv[argv.index(flag)+1] == value
    assert '--read-only' in argv and '--privileged' not in argv
    assert not any('/var/run' in arg or '/home/' in arg for arg in argv)
    assert argv[-3:] == [p.image, '-c', 'true']


def test_wrong_container_owner_never_removed(monkeypatch):
    calls = []
    def fake(argv, **kw):
        calls.append(argv)
        if 'ps' in argv: return b'abc\n'
        return json.dumps([dict(Id='abc', Name='/swb-test', Config={'Labels': {}})]).encode()
    monkeypatch.setattr(w, 'command', fake)
    with pytest.raises(w.WorkerError, match='ownership'):
        w.inspect_container(['docker'], dict(attempt_id='test', token='owned', container_name='swb-test'))
    assert all('rm' not in call for call in calls)


def test_lock_is_exclusive(state):
    with w.attempt_lock(state):
        with pytest.raises(w.ControllerActive):
            with w.attempt_lock(state): pass


live = pytest.mark.skipif(os.environ.get('WB_TEST_DOCKER') != '1', reason='explicit Docker integration opt-in required')


def run(repo, state, script, **kwargs):
    return w.run_worker(profile(), repo, 'HEAD', state, 'synthetic-test',
                        ['sh', '-c', script], sudo=os.environ.get('WB_TEST_DOCKER_SUDO') == '1', **kwargs)


@live
def test_live_clean_success_and_recovery(repo, state):
    result = run(repo, state, 'test "$(id -u)" != 0 && test ! -S /var/run/docker.sock && '
                 '! touch /root/forbidden && ! touch /.forbidden && '
                 'test "$(cat /workspace/.git)" = "Git administration is host-owned." && '
                 'echo out && echo err >&2')
    assert result['phase'] == 'exited', result
    assert result['exit_code'] == 0 and result['cleanup'] == 'complete', result
    assert not Path(result['worktree']).exists()
    assert w.recover(result['attempt_path'], sudo=os.environ.get('WB_TEST_DOCKER_SUDO') == '1') == result
    output = (Path(result['artifact_manifest']).parent/'output.txt').read_text()
    assert 'out' in output and 'err' in output
    assert w.inspect_container(w.docker_prefix(os.environ.get('WB_TEST_DOCKER_SUDO') == '1'), result) is None


@live
def test_live_concurrent_changes_isolated_and_preserved(repo, state):
    def worker(value):
        return run(repo, state, f'echo {value} > hello.txt; echo result-{value} > report.txt', artifact_paths=['report.txt'])
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        a,b = list(pool.map(worker, ['one', 'two']))
    assert a['attempt_id'] != b['attempt_id']
    for value, result in [('one', a), ('two', b)]:
        assert result['exit_code'] == 0 and result['cleanup'] == 'retained', result
        assert Path(result['worktree'], 'hello.txt').read_text().strip() == value
        destination = Path(result['artifact_manifest']).parent
        assert (destination/'file-0').read_text().strip() == 'result-'+value
        assert ('+'+value) in (destination/'changes.patch').read_text()
        before = (destination/'manifest.json').read_bytes()
        w.recover(result['attempt_path'], sudo=os.environ.get('WB_TEST_DOCKER_SUDO') == '1')
        assert (destination/'manifest.json').read_bytes() == before
        assert w.inspect_container(w.docker_prefix(os.environ.get('WB_TEST_DOCKER_SUDO') == '1'), result) is None
        # Fixture teardown only: explicit discard of this synthetic test's changes.
        w.git(repo, 'worktree', 'remove', '--force', result['worktree'])
    assert (repo/'hello.txt').read_text() == 'original\n'


@live
def test_live_failed_start(repo, state):
    result = w.run_worker(profile(), repo, 'HEAD', state, 'synthetic', ['/missing-program'], sudo=os.environ.get('WB_TEST_DOCKER_SUDO') == '1')
    assert result['phase'] in {'start_failed', 'exited'}, result
    assert result.get('exit_code', 1) != 0, result
    assert result['cleanup'] == 'complete', result
    assert w.inspect_container(w.docker_prefix(os.environ.get('WB_TEST_DOCKER_SUDO') == '1'), result) is None


@live
def test_live_timeout_and_cancel(repo, state):
    p = profile(); p['timeout_seconds'] = 1
    result = w.run_worker(p, repo, 'HEAD', state, 'synthetic', ['sleep', '30'], sudo=os.environ.get('WB_TEST_DOCKER_SUDO') == '1')
    assert result['phase'] == 'cancelled' and result['reason'] == 'timeout', result
    assert result['cleanup'] == 'complete', result
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        future = pool.submit(run, repo, state, 'sleep 30')
        deadline = time.monotonic()+15
        while True:
            attempts = list(state.glob('*/worker.json'))
            active = [p for p in attempts if json.loads(p.read_text())['phase'] == 'running']
            if active: break
            assert time.monotonic() < deadline
            time.sleep(.1)
        w.atomic(active[0].parent/'cancel.request', {'requested_at': w.stamp()})
        result = future.result(timeout=15)
    assert result['phase'] == 'cancelled' and result['reason'] == 'operator_cancel', result
    assert result['cleanup'] == 'complete', result


@live
def test_live_rejected_create_cleanup(repo, state, monkeypatch):
    original = w.command
    def fail_create(argv, **kw):
        if 'create' in argv and 'docker' in argv:
            raise w.WorkerError('Synthetic create failure')
        return original(argv, **kw)
    monkeypatch.setattr(w, 'command', fail_create)
    result = run(repo, state, 'true')
    assert result['phase'] == 'start_failed', result
    assert result['cleanup'] == 'complete', result
    assert not Path(result['worktree']).exists()


@live
def test_live_controller_crash_recovery(repo, state, tmp_path):
    config = tmp_path/'profile.json'; config.write_text(json.dumps(profile()))
    prefix = [sys.executable, '-m', 'starforge_workbench.docker_worker']
    if os.environ.get('WB_TEST_DOCKER_SUDO') == '1': prefix.append('--sudo')
    process = subprocess.Popen(prefix + ['run', '--profile', str(config), '--repository', str(repo),
        '--revision', 'HEAD', '--state-root', str(state), '--task', 'synthetic-crash', '--', 'sleep', '30'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic()+15
        while True:
            active = [p for p in state.glob('*/worker.json') if json.loads(p.read_text())['phase'] == 'running']
            if active: break
            assert process.poll() is None and time.monotonic() < deadline
            time.sleep(.1)
        process.kill(); process.wait(timeout=5)
        result = w.recover(active[0].parent, sudo=os.environ.get('WB_TEST_DOCKER_SUDO') == '1', cancel=True)
        assert result['phase'] == 'cancelled' and result['cleanup'] == 'complete', result
    finally:
        if process.poll() is None: process.kill(); process.wait()


@live
def test_live_symlink_artifact_retained(repo, state):
    result = run(repo, state, 'ln -s /etc/passwd report.txt', artifact_paths=['report.txt'])
    assert result['artifacts'] == 'incomplete' and result['cleanup'] == 'retained', result
    assert Path(result['worktree']).exists()
    # Repair the synthetic declaration and demonstrate safe recovery.
    Path(result['worktree'], 'report.txt').unlink()
    Path(result['worktree'], 'report.txt').write_text('safe result')
    result = w.recover(result['attempt_path'], sudo=os.environ.get('WB_TEST_DOCKER_SUDO') == '1')
    assert result['artifacts'] == 'complete', result
    w.git(repo, 'worktree', 'remove', '--force', result['worktree'])


@pytest.mark.parametrize('entries', [
    ['TOKEN=synthetic-private-value'], ['LD_PRELOAD=/tmp/inject.so'],
    ['PATH=/scratch/bin:/usr/bin'], ['HOME=/home/private'],
    ['LANG=C', 'LANG=C'], ['PYTHON_VERSION=synthetic-private-value'],
    ['GPG_KEY=synthetic-private-value'], ['LANG=C\n'], ['broken'], [None],
    {'TOKEN': 'synthetic-private-value'}, ['LANG='+'x'*257],
])
def test_image_environment_refused_before_allocation(entries, repo, state, monkeypatch):
    original = w.command
    calls = []
    def fake(argv, **kwargs):
        calls.append(argv)
        if 'docker' not in argv:
            return original(argv, **kwargs)
        if 'info' in argv:
            return json.dumps({'NCPU': 2, 'MemTotal': 1024**3}).encode()
        if 'image' in argv:
            return json.dumps([{'Id': 'synthetic', 'Config': {'Env': entries}}]).encode()
        pytest.fail('Unexpected Docker mutation')
    monkeypatch.setattr(w, 'command', fake)
    with pytest.raises(w.WorkerError) as error:
        w.run_worker(profile(), repo, 'HEAD', state, 'synthetic', ['true'])
    assert 'synthetic-private-value' not in str(error.value)
    assert not list(state.iterdir())
    assert not any('create' in call or 'start' in call for call in calls)


def test_environment_fingerprint_and_fixed_overrides():
    runtime = [key+'='+value for key, value in w.RUNTIME_ENV.items()]
    assert w.environment_digest(None, image=True) == w.environment_digest(runtime)
    assert w.environment_digest(['HOME=/root', 'LANG=C'], image=True) == w.environment_digest(runtime)
    assert w.environment_digest(['PATH=/usr/local/bin:'+w.RUNTIME_ENV['PATH']], image=True) == w.environment_digest(runtime)
    toolchain = ['PYTHON_VERSION=3.14.7', 'PYTHON_SHA256='+'a'*64, 'GPG_KEY='+'B'*40]
    digest = w.environment_digest(toolchain, image=True)
    assert digest == w.environment_digest(list(reversed(runtime+toolchain)))
    assert digest != w.environment_digest(runtime+['PYTHON_VERSION=3.11.16'])
    for entries in (None, [], ['HOME=/root'], runtime+['TOKEN=synthetic-private-value']):
        with pytest.raises(w.WorkerError):
            w.environment_digest(entries)


@live
def test_live_fixed_environment_and_no_host_credential_inheritance(repo, state, monkeypatch):
    monkeypatch.setenv('SYNTHETIC_HOST_CREDENTIAL', 'synthetic-private-value')
    result = run(repo, state, 'test -z "$SYNTHETIC_HOST_CREDENTIAL" && '
                 'test "$HOME" = /tmp && test "$LANG" = C.UTF-8 && '
                 'test "$PATH" = /usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin')
    assert result['exit_code'] == 0 and result['cleanup'] == 'complete', result
    assert 'synthetic-private-value' not in json.dumps(result)
    for path in Path(result['attempt_path']).rglob('*'):
        if path.is_file():
            assert b'synthetic-private-value' not in path.read_bytes()


@live
@pytest.mark.parametrize('mutation', ['unapproved', 'missing', 'changed'])
def test_live_environment_mismatch_never_starts(repo, state, monkeypatch, mutation):
    inspect = w.inspect_container
    original = w.command
    calls = []
    def altered(docker, receipt):
        item = inspect(docker, receipt)
        if item and receipt['phase'] == 'starting':
            if mutation == 'unapproved':
                item['Config']['Env'].append('TOKEN=synthetic-private-value')
            elif mutation == 'missing':
                item['Config']['Env'] = []
            else:
                item['Config']['Env'].append('PYTHON_VERSION=3.14.7')
        return item
    def capture(argv, **kwargs):
        calls.append(argv)
        return original(argv, **kwargs)
    monkeypatch.setattr(w, 'inspect_container', altered)
    monkeypatch.setattr(w, 'command', capture)
    result = run(repo, state, 'echo SHOULD_NOT_RUN')
    assert result['phase'] == 'start_failed' and result['cleanup'] == 'complete', result
    assert not any('docker' in call and 'start' in call for call in calls)
    assert 'synthetic-private-value' not in json.dumps(result)
