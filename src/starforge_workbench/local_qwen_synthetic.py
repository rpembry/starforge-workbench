"""Opt-in installed-model smoke adapter, restricted to the fixed synthetic fixture.

Trusted local code supplies installed files. No cloud request can choose a model,
prompt, executable or endpoint. A fresh transient user service owns the only runner;
the shared Ollama service is never contacted or modified.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import tempfile
import time
import uuid

from .diagnostic_batch import MockQwenAdapter, SYNTHETIC_FIXTURE
from .docker_worker import atomic, attempt_lock, private_directory

MODEL_SHA256 = 'a3de86cd1c132c822487ededd47a324c50491393e6565cd14bafa40d0b8e686f'
MODEL_BYTES = 5225374496
MAX_RESPONSE = 16384
MEMORY_BYTES = 8 * 1024**3
WALL_SECONDS = 60
CGROUP_ROOT = Path('/sys/fs/cgroup')
PROMPT = (
    '<|im_start|>system\nReturn only JSON with summary (string) and findings '
    '(array of strings). No tools. This is a synthetic capacity diagnostic.\n<|im_end|>\n'
    '<|im_start|>user\nSynthetic configured workers=6; available capacity=4. '
    'State the capacity deficit and recommended local configuration review.\n<|im_end|>\n'
    '<|im_start|>assistant\n<think>\n\n</think>\n\n'
)


class LocalModelError(RuntimeError):
    def __init__(self):
        super().__init__('Synthetic local inference unavailable')


def _installed_file(path):
    path = Path(path).absolute()
    if path.is_symlink() or not path.is_file() or path.resolve() != path:
        raise LocalModelError()
    return path


def launch_command(runner, model, scratch, unit):
    """Owner-only composition; no free-form operation or forwarded environment."""
    if not unit.startswith('sf-qwen-synthetic-') or not unit.endswith('.service'):
        raise LocalModelError()
    return [
        '/usr/bin/systemd-run', '--user', '--quiet', '--wait', '--pipe', '--collect',
        '--service-type=exec', '--unit='+unit,
        '-p', 'MemoryMax='+str(MEMORY_BYTES), '-p', 'MemorySwapMax=0',
        '-p', 'CPUQuota=200%', '-p', 'RuntimeMaxSec=60', '-p', 'TimeoutStopSec=2',
        '-p', 'KillMode=control-group', '-p', 'TasksMax=64', '-p', 'Nice=10',
        '/usr/bin/bwrap', '--unshare-all', '--die-with-parent', '--new-session',
        '--cap-drop', 'ALL', '--ro-bind', '/usr', '/usr',
        '--symlink', 'usr/bin', '/bin', '--symlink', 'usr/lib64', '/lib64',
        '--symlink', 'usr/lib', '/lib', '--proc', '/proc', '--dev', '/dev',
        '--tmpfs', '/tmp', '--ro-bind', str(runner.parent), '/runner',
        '--ro-bind', str(model), '/model.gguf', '--bind', str(scratch), '/scratch',
        '--clearenv', '--setenv', 'PATH', '/usr/bin',
        '--setenv', 'LD_LIBRARY_PATH', '/runner', '--setenv', 'HOME', '/tmp',
        '/runner/'+runner.name, '-m', '/model.gguf', '--host', '/scratch/qwen.sock',
        '-t', '2', '-tb', '2', '-c', '2048', '-n', '256', '-np', '1',
        '-b', '128', '-ub', '128', '--device', 'none', '-ngl', '0',
        '--no-op-offload', '--no-kv-offload', '--fit', 'off', '--reasoning', 'off',
    ]


def _response_json(response):
    """Bounded non-streaming HTTP/1.x JSON; transfer encodings are unsupported.

    Only connection-close or one exact Content-Length frame is accepted. This
    deliberately does not implement a general HTTP/chunked/compression client.
    Header/body parse failures never return server-controlled exception text.
    """
    try:
        if type(response) is not bytes or len(response) > MAX_RESPONSE:
            raise LocalModelError()
        headers, separator, data = response.partition(b'\r\n\r\n')
        lines = headers.split(b'\r\n')
        if not separator or not re.fullmatch(rb'HTTP/1\.[01] 200(?: [\x20-\x7e]*)?', lines[0]):
            raise LocalModelError()
        length = None
        for line in lines[1:]:
            name, colon, value = line.partition(b':')
            if (not colon or not re.fullmatch(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name)
                    or not re.fullmatch(rb'[\t\x20-\x7e]*', value)):
                raise LocalModelError()
            name, value = name.lower(), value.strip(b' \t')
            if name == b'transfer-encoding':
                # Includes chunked, identity, unknown and CL/TE ambiguity.
                raise LocalModelError()
            if name == b'content-encoding' and value.lower() != b'identity':
                raise LocalModelError()
            if name == b'content-length':
                if (length is not None or len(value) > 5
                        or not re.fullmatch(rb'[0-9]+', value)):
                    raise LocalModelError()
                length = int(value)
                if length > MAX_RESPONSE:
                    raise LocalModelError()
        if length is not None and len(data) != length:
            raise LocalModelError()
        return json.loads(data)
    except Exception:
        raise LocalModelError() from None


def unix_request(path, route, body=None, timeout=1):
    """Owned-socket IPC only; bounded close/fixed-length framing, no chunked support."""
    payload = b'' if body is None else json.dumps(body).encode()
    method = 'GET' if body is None else 'POST'
    request = (f'{method} {route} HTTP/1.0\r\nHost: localhost\r\n'
               f'Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n\r\n').encode()+payload
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
            deadline = time.monotonic()+timeout
            stream.settimeout(timeout)
            stream.connect(str(path))
            stream.sendall(request)
            response = bytearray()
            while True:
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise LocalModelError()
                stream.settimeout(remaining)
                part = stream.recv(min(4096, MAX_RESPONSE+1-len(response)))
                if not part:
                    break
                response.extend(part)
                if len(response) > MAX_RESPONSE:
                    raise LocalModelError()
        return _response_json(bytes(response))
    except Exception:
        raise LocalModelError() from None


def stop_owned(unit, process):
    """Always reap our helper; failed stop never counts as successful cleanup."""
    stopped = False
    try:
        result = subprocess.run(['/usr/bin/systemctl', '--user', 'stop', unit],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        stopped = result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        pass
    finally:
        if not stopped:
            # Only the private UUID service, never a shared server or guest.
            try:
                subprocess.run(['/usr/bin/systemctl', '--user', 'kill', '--kill-whom=all', unit],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
                result = subprocess.run(['/usr/bin/systemctl', '--user', 'stop', unit],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
                stopped = result.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                stopped = False
    if not stopped:
        raise LocalModelError()


def verify_kernel_limits(control_group):
    if (type(control_group) is not str or not control_group.startswith('/')
            or '..' in Path(control_group).parts):
        raise LocalModelError()
    path = CGROUP_ROOT/control_group.lstrip('/')
    if path.resolve() != path:
        raise LocalModelError()
    if ((path/'memory.max').read_text().strip() != str(MEMORY_BYTES)
            or (path/'memory.swap.max').read_text().strip() != '0'):
        raise LocalModelError()
    quota, period = map(int, (path/'cpu.max').read_text().split())
    if quota <= 0 or period <= 0 or quota > 2*period:
        raise LocalModelError()


def slot_available(directory):
    path = directory/'inference-slot.json'
    if not path.exists() and not path.is_symlink():
        return True
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 1024):
                raise LocalModelError()
            state = json.loads(stream.read(1025))
        return (type(state) is dict and set(state) == {'version', 'phase', 'unit'}
                and state['version'] == 1 and state['phase'] == 'stopped')
    except Exception:
        raise LocalModelError() from None


class LocalQwen8BAdapter(MockQwenAdapter):
    """Explicit synthetic adapter; no VM data or arbitrary fixture accepted."""
    def __init__(self, runner, model, *, inference_slot):
        self.runner, self.model = _installed_file(runner), _installed_file(model)
        # The trusted owner supplies the common slot for every local adapter.
        self.inference_slot = private_directory(inference_slot)
        if self.model.stat().st_size != MODEL_BYTES:
            raise LocalModelError()
        with self.model.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != MODEL_SHA256:
                raise LocalModelError()
        self.evidence = None

    def _prompt(self):
        """Trusted registered synthetic adapters may specialize typed fixture data."""
        return PROMPT

    def analyze(self, fixture):
        self.evidence = None
        if fixture != SYNTHETIC_FIXTURE:
            raise LocalModelError()
        with attempt_lock(self.inference_slot):
            if not slot_available(self.inference_slot):
                raise LocalModelError()
            return self._analyze_owned()

    def _analyze_owned(self):
        # Leave >=16 GiB for existing workloads and the separately owned guest.
        mem = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
        if int(mem['MemAvailable'].split()[0])*1024 < MEMORY_BYTES+16*1024**3:
            raise LocalModelError()
        unit = 'sf-qwen-synthetic-'+uuid.uuid4().hex+'.service'
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix='sf-qwen-synthetic-') as temporary:
            scratch = Path(temporary)
            scratch.chmod(0o700)
            slot = {'version': 1, 'phase': 'intent', 'unit': unit}
            atomic(self.inference_slot/'inference-slot.json', slot)
            process = subprocess.Popen(launch_command(self.runner, self.model, scratch, unit),
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                path = scratch/'qwen.sock'
                deadline = started+WALL_SECONDS
                while True:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise LocalModelError()
                    try:
                        if unix_request(path, '/health')['status'] == 'ok':
                            break
                    except (OSError, LocalModelError, ValueError, KeyError):
                        time.sleep(.1)
                # Verify live service limits rather than trusting command construction.
                result = subprocess.run(['/usr/bin/systemctl', '--user', 'show', unit,
                                         '-p', 'MemoryMax', '-p', 'MemorySwapMax',
                                         '-p', 'CPUQuotaPerSecUSec', '-p', 'RuntimeMaxUSec',
                                         '-p', 'ControlGroup'], capture_output=True, timeout=3, check=True)
                props = dict(line.split('=', 1) for line in result.stdout.decode().splitlines())
                if (props.get('MemoryMax') != str(MEMORY_BYTES) or props.get('MemorySwapMax') != '0'
                        or props.get('CPUQuotaPerSecUSec') != '2s' or props.get('RuntimeMaxUSec') != '1min'):
                    raise LocalModelError()
                verify_kernel_limits(props.get('ControlGroup'))
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise LocalModelError()
                response = unix_request(path, '/completion', {
                    'prompt': self._prompt(), 'n_predict': 256, 'temperature': 0, 'stream': False,
                    'json_schema': {'type': 'object', 'properties': {
                        'summary': {'type': 'string'},
                        'findings': {'type': 'array', 'items': {'type': 'string'}}},
                        'required': ['summary', 'findings'], 'additionalProperties': False}},
                    timeout=remaining)
                report = json.loads(response['content'])
                if (type(report) is not dict or set(report) != {'summary', 'findings'}
                        or type(report['summary']) is not str or len(report['summary']) > 4096
                        or type(report['findings']) is not list or len(report['findings']) > 16
                        or any(type(item) is not str or len(item) > 4096 for item in report['findings'])):
                    raise LocalModelError()
                self.evidence = {'model_sha256': MODEL_SHA256, 'model_bytes': MODEL_BYTES,
                                 'cpu_threads': 2, 'memory_max': MEMORY_BYTES, 'swap_max': 0,
                                 'cpu_quota': '200%', 'context': 2048, 'output_tokens_max': 256,
                                 'wall_seconds_max': WALL_SECONDS, 'gpu': False,
                                 'network_namespace': 'private', 'ipc': 'owned_unix_socket',
                                 'kernel_limits_verified': True,
                                 'elapsed_seconds': round(time.monotonic()-started, 3)}
                return report
            except Exception:
                raise LocalModelError() from None
            finally:
                # Only the UUID unit created above is stopped; never a shared server/guest.
                try:
                    stop_owned(unit, process)
                    slot['phase'] = 'stopped'
                    atomic(self.inference_slot/'inference-slot.json', slot)
                except Exception:
                    self.evidence = None
                    slot['phase'] = 'uncertain'
                    atomic(self.inference_slot/'inference-slot.json', slot)
                    raise LocalModelError() from None
