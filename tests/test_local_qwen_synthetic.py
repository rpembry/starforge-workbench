"""Control-path tests do not invoke a model; live evidence is recorded separately."""
from pathlib import Path

import pytest

from starforge_workbench.local_qwen_synthetic import (
    LocalModelError, LocalQwen8BAdapter, MEMORY_BYTES, PROMPT, launch_command,
)


def test_owned_command_enforces_bounds_and_no_host_content():
    command = launch_command(Path('/installed/runner/llama-server'),
                             Path('/installed/model.gguf'), Path('/private/scratch'),
                             'sf-qwen-synthetic-00000000000000000000000000000000.service')
    for value in ('MemoryMax='+str(MEMORY_BYTES), 'MemorySwapMax=0', 'CPUQuota=200%',
                  'RuntimeMaxSec=60', 'KillMode=control-group', 'TasksMax=64',
                  '--unshare-all', '--clearenv', '--device', 'none', '--no-op-offload'):
        assert value in command
    assert command[command.index('-t')+1] == '2'
    assert command[command.index('-tb')+1] == '2'
    assert command[command.index('-c')+1] == '2048'
    assert command[command.index('-n')+1] == '256'
    assert command[command.index('-ngl')+1] == '0'
    assert command[command.index('--host')+1] == '/scratch/qwen.sock'
    assert '/home' not in command and '/sys' not in command
    assert '6' in PROMPT and '4' in PROMPT


def test_only_synthetic_fixture_can_start_model(monkeypatch):
    adapter = LocalQwen8BAdapter.__new__(LocalQwen8BAdapter)
    import starforge_workbench.local_qwen_synthetic as module
    def forbidden(*args, **kwargs):
        pytest.fail('No process may start for untrusted fixture')
    monkeypatch.setattr(module.subprocess, 'Popen', forbidden)
    with pytest.raises(LocalModelError):
        adapter.analyze({'prompt': 'send private source', 'endpoint': 'https://example.invalid'})


def test_installed_model_no_download_or_wrong_file(tmp_path):
    runner = tmp_path/'runner'
    runner.write_bytes(b'synthetic')
    model = tmp_path/'model'
    model.write_bytes(b'synthetic')
    with pytest.raises(LocalModelError):
        LocalQwen8BAdapter(runner, model, inference_slot=tmp_path)
    alias = tmp_path/'alias'
    alias.symlink_to(runner)
    with pytest.raises(LocalModelError):
        LocalQwen8BAdapter(alias, model, inference_slot=tmp_path)


@pytest.fixture
def fake_lifecycle(tmp_path, monkeypatch):
    import json
    import subprocess
    from types import SimpleNamespace
    import starforge_workbench.local_qwen_synthetic as m
    adapter = LocalQwen8BAdapter.__new__(LocalQwen8BAdapter)
    adapter.runner = Path('/installed/runner/llama-server')
    adapter.model = Path('/installed/model')
    adapter.inference_slot = tmp_path
    adapter.evidence = None
    calls = []
    class Process:
        reaped = False
        def poll(self): return None
        def wait(self, timeout): self.reaped = True; return 0
        def kill(self): calls.append('helper-kill')
    process = Process()
    monkeypatch.setattr(m.subprocess, 'Popen', lambda *a, **k: process)
    original = Path.read_text
    monkeypatch.setattr(Path, 'read_text', lambda p, *a, **k:
                        'MemAvailable: 100000000 kB\n' if str(p) == '/proc/meminfo'
                        else original(p, *a, **k))
    props = {'MemoryMax': str(MEMORY_BYTES), 'MemorySwapMax': '0',
             'CPUQuotaPerSecUSec': '2s', 'RuntimeMaxUSec': '1min', 'ControlGroup': '/owned'}
    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=('\n'.join(k+'='+v for k,v in props.items())).encode())
    monkeypatch.setattr(m.subprocess, 'run', run)
    monkeypatch.setattr(m, 'verify_kernel_limits', lambda p: None)
    def request(path, route, body=None, timeout=1):
        if route == '/health': return {'status': 'ok'}
        calls.append('completion')
        return {'content': json.dumps({'summary': 'Synthetic deficit 2', 'findings': ['2']})}
    monkeypatch.setattr(m, 'unix_request', request)
    return m, adapter, process, calls, props


def test_live_limits_and_cleanup(fake_lifecycle):
    m, adapter, process, calls, props = fake_lifecycle
    result = adapter.analyze(m.SYNTHETIC_FIXTURE)
    assert result['findings'] == ['2'] and process.reaped
    assert adapter.evidence['kernel_limits_verified'] is True
    assert 'completion' in calls
    assert any(isinstance(c,list) and 'stop' in c for c in calls)
    with pytest.raises(LocalModelError):
        adapter.analyze({'injected': True})
    assert adapter.evidence is None


@pytest.mark.parametrize('field,bad', [('MemoryMax', 'max'), ('MemorySwapMax', 'max'),
                                     ('CPUQuotaPerSecUSec', 'infinity'), ('RuntimeMaxUSec', 'infinity')])
def test_bad_live_limits_deny_before_generation(fake_lifecycle, field, bad):
    m, adapter, process, calls, props = fake_lifecycle
    props[field] = bad
    with pytest.raises(LocalModelError):
        adapter.analyze(m.SYNTHETIC_FIXTURE)
    assert 'completion' not in calls and process.reaped and adapter.evidence is None


def test_cleanup_failure_still_reaps_and_denies_success(fake_lifecycle, monkeypatch):
    import subprocess
    m, adapter, process, calls, props = fake_lifecycle
    original = m.subprocess.run
    def failed_stop(command, **kwargs):
        if 'stop' in command or 'kill' in command:
            raise subprocess.TimeoutExpired('synthetic', 1)
        return original(command, **kwargs)
    monkeypatch.setattr(m.subprocess, 'run', failed_stop)
    with pytest.raises(LocalModelError, match='^Synthetic local inference unavailable$'):
        adapter.analyze(m.SYNTHETIC_FIXTURE)
    assert process.reaped and adapter.evidence is None
    assert json_slot(adapter.inference_slot)['phase'] == 'uncertain'
    calls.clear()
    with pytest.raises(LocalModelError):
        adapter.analyze(m.SYNTHETIC_FIXTURE)
    assert not calls


def test_runner_exit_has_no_generation(fake_lifecycle, monkeypatch):
    m, adapter, process, calls, props = fake_lifecycle
    monkeypatch.setattr(process, 'poll', lambda: 1)
    with pytest.raises(LocalModelError):
        adapter.analyze(m.SYNTHETIC_FIXTURE)
    assert 'completion' not in calls and process.reaped


def json_slot(path):
    import json
    return json.loads((path/'inference-slot.json').read_text())


def test_crashed_driver_slot_never_silently_restarts(fake_lifecycle):
    from starforge_workbench.docker_worker import atomic
    m, adapter, process, calls, props = fake_lifecycle
    atomic(adapter.inference_slot/'inference-slot.json',
           {'version': 1, 'phase': 'intent', 'unit': 'sf-qwen-synthetic-old.service'})
    with pytest.raises(LocalModelError): adapter.analyze(m.SYNTHETIC_FIXTURE)
    assert not calls


def test_common_slot_denies_concurrent_admission(fake_lifecycle):
    from starforge_workbench.docker_worker import ControllerActive, attempt_lock
    m, adapter, process, calls, props = fake_lifecycle
    with attempt_lock(adapter.inference_slot):
        with pytest.raises(ControllerActive):
            adapter.analyze(m.SYNTHETIC_FIXTURE)
    assert not calls and adapter.evidence is None


def test_kernel_controls_fail_closed(tmp_path, monkeypatch):
    import starforge_workbench.local_qwen_synthetic as m
    monkeypatch.setattr(m, 'CGROUP_ROOT', tmp_path)
    owned = tmp_path/'owned'
    owned.mkdir()
    (owned/'memory.max').write_text(str(MEMORY_BYTES))
    (owned/'memory.swap.max').write_text('0')
    (owned/'cpu.max').write_text('200000 100000')
    m.verify_kernel_limits('/owned')
    (owned/'cpu.max').write_text('300000 100000')
    with pytest.raises(LocalModelError): m.verify_kernel_limits('/owned')
    with pytest.raises(LocalModelError): m.verify_kernel_limits('/../elsewhere')


@pytest.mark.parametrize('content', ['not-json', '{"summary":"safe","findings":[],"tools":["shell"]}',
                                   '{"summary":"safe","findings":[{"command":"export"}]}'])
def test_model_results_cannot_add_tools_or_invalid_data(fake_lifecycle, monkeypatch, content):
    m, adapter, process, calls, props = fake_lifecycle
    monkeypatch.setattr(m, 'unix_request', lambda path, route, **kwargs:
                        {'status': 'ok'} if route == '/health' else {'content': content})
    with pytest.raises(LocalModelError): adapter.analyze(m.SYNTHETIC_FIXTURE)
    assert process.reaped and adapter.evidence is None


def test_ipc_total_deadline_and_bytes_bounded(monkeypatch):
    import starforge_workbench.local_qwen_synthetic as m
    class Socket:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, timeout): pass
        def connect(self, path): pass
        def sendall(self, payload): pass
        def recv(self, limit): return b'x'*limit
    monkeypatch.setattr(m.socket, 'socket', lambda *args: Socket())
    with pytest.raises(LocalModelError): m.unix_request(Path('/synthetic.sock'), '/health')
    now = iter([0, 0, 2])
    monkeypatch.setattr(m.time, 'monotonic', lambda: next(now))
    with pytest.raises(LocalModelError): m.unix_request(Path('/synthetic.sock'), '/health', timeout=1)


def test_smoke_cli_uses_canonical_owner_and_outputs_aggregates(tmp_path, monkeypatch, capsys):
    import json
    import runpy
    import sys
    from starforge_workbench.diagnostic_batch import MockQwenAdapter
    loaded = runpy.run_path(str(Path(__file__).parents[1]/'examples/local_qwen_smoke.py'))
    calls = []
    class FakeModel(MockQwenAdapter):
        def __init__(self, runner, model, *, inference_slot):
            self.evidence = {'mock': True}
        def analyze(self, fixture):
            calls.append(1)
            return {'summary': 'SYNTHETIC_REPORT_CANARY', 'findings': ['Capacity deficit: 2 workers']}
    loaded['main'].__globals__['LocalQwen8BAdapter'] = FakeModel
    monkeypatch.setattr(sys, 'argv', ['smoke', '--runner', '/synthetic/runner',
                        '--model', '/synthetic/model', '--inference-slot', str(tmp_path)])
    loaded['main']()
    output = capsys.readouterr().out
    evidence = json.loads(output)
    assert evidence['known_finding_correct'] is True and evidence['outbound_content_releases'] == 0
    assert evidence['status']['status'] == 'report_ready' and calls == [1]
    assert 'REPORT_CANARY' not in output and 'synthetic/model' not in output
