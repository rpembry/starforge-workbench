"""Opt-in, host-controlled Docker task attempts; persistent launchers are unchanged."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone

from .execution import ProfileError, load_profile, parse_profile, prepare_execution

LABEL = 'io.starforge.worker'
MAX_ARTIFACT = 8 * 1024 * 1024


class WorkerError(RuntimeError):
    pass


class ControllerActive(WorkerError):
    pass


def stamp():
    return datetime.now(timezone.utc).isoformat()


def atomic(path, data):
    fd, temporary = tempfile.mkstemp(prefix='.receipt-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, sort_keys=True)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def private_directory(path):
    path = Path(path).absolute()
    if path.resolve() != path or not path.is_dir():
        raise WorkerError('Expected an existing nonsymlink directory')
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise WorkerError('Attempt directories must be caller-owned and private')
    if any(c in str(path) for c in ':,\n\r'):
        raise WorkerError('Unsupported allocation path characters')
    return path


def command(argv, timeout=30, limit=MAX_ARTIFACT, combined=False):
    """Drain bounded output without a shell, inherited Git overrides, or disk spooling."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(('GIT_', 'DOCKER_'))}
    try:
        with subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=env) as process:
            output = bytearray()
            count = 0
            deadline = time.monotonic() + timeout
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    selector.register(process.stderr, selectors.EVENT_READ)
                    while selector.get_map():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise WorkerError('Command timed out')
                        for key, _ in selector.select(min(remaining, .1)):
                            data = os.read(key.fileobj.fileno(), 65536)
                            if not data:
                                selector.unregister(key.fileobj)
                                continue
                            count += len(data)
                            if count > limit:
                                raise WorkerError('Command output exceeds artifact limit')
                            if combined or key.fileobj is process.stdout:
                                output.extend(data)
                result = process.wait(timeout=max(.01, deadline - time.monotonic()))
            except BaseException:
                process.kill()
                process.wait()
                raise
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkerError('Command unavailable or timed out') from exc
    if result:
        raise WorkerError('Command failed (exit '+str(result)+')')
    return bytes(output)


def git(repo, *args):
    return command(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
                    '-C', str(repo), *args])


def docker_prefix(sudo=False):
    # Explicit local socket; do not inherit a remote DOCKER_HOST/context.
    return (['sudo', '-n'] if sudo else []) + ['docker', '--host', 'unix:///var/run/docker.sock']


def inspect_container(docker, receipt):
    ids = command(docker + ['ps', '-aq', '--no-trunc', '--filter',
                            'label='+LABEL+'.attempt='+receipt['attempt_id']]).decode().split()
    if not ids:
        return None
    if len(ids) != 1:
        raise WorkerError('Ambiguous container ownership; nothing removed')
    item = json.loads(command(docker + ['inspect', ids[0]]))[0]
    labels = item['Config'].get('Labels') or {}
    if (labels.get(LABEL+'.token') != receipt['token'] or
        item['Name'].lstrip('/') != receipt['container_name'] or
        (receipt.get('container_id') and item['Id'] != receipt['container_id'])):
        raise WorkerError('Container ownership mismatch; nothing removed')
    return item


# No host environment is forwarded into the container. Image defaults are input,
# too: accept only these non-secret, bounded runtime/toolchain settings.
RUNTIME_ENV = {
    'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
    'HOME': '/tmp',
    'LANG': 'C.UTF-8',
}
IMAGE_ENV_PATTERNS = {
    'PATH': r'(?:/usr/local/bin:)?/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
    'HOME': r'/(?:tmp|root)',
    'LANG': r'(?:C|C\.UTF-8|en_US\.UTF-8)',
    'PYTHON_VERSION': r'[0-9]{1,2}\.[0-9]{1,2}\.[0-9]{1,3}(?:(?:a|b|rc)[0-9]{1,2})?',
    'PYTHON_SHA256': r'[a-fA-F0-9]{64}',
    'GPG_KEY': r'[a-fA-F0-9]{40}',
}


def environment_digest(entries, *, image=False):
    """Validate without echoing/storing values; fingerprint the effective env."""
    if entries is None and image:
        entries = []
    if not isinstance(entries, list) or len(entries) > len(IMAGE_ENV_PATTERNS):
        raise WorkerError('Environment policy refused; use an image with approved runtime defaults only')
    values = {}
    for entry in entries:
        if not isinstance(entry, str) or len(entry) > 256 or '=' not in entry:
            raise WorkerError('Malformed environment entry; use NAME=value runtime defaults')
        key, value = entry.split('=', 1)
        pattern = IMAGE_ENV_PATTERNS.get(key)
        if key in values or pattern is None or re.fullmatch(pattern, value) is None:
            raise WorkerError('Environment policy refused; remove unapproved or duplicate image defaults')
        values[key] = value
    if image:
        values.update(RUNTIME_ENV)
    elif any(values.get(key) != value for key, value in RUNTIME_ENV.items()):
        raise WorkerError('Container environment does not match fixed runtime defaults')
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def create_argv(docker, receipt, profile):
    attempt = Path(receipt['attempt_path'])
    args = docker + ['create', '--pull', 'never', '--name', receipt['container_name'],
        '--label', LABEL+'.attempt='+receipt['attempt_id'], '--label', LABEL+'.token='+receipt['token'],
        '--user', f'{profile.user}:{profile.group}', '--network', 'none', '--ipc', 'private',
        '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--read-only', '--restart', 'no',
        '--cpus', str(profile.cpus), '--memory', str(profile.memory_mb)+'m',
        '--memory-swap', str(profile.memory_mb)+'m', '--pids-limit', str(profile.pids_limit),
        '--init', '--no-healthcheck', '--stop-timeout', '5', '--workdir', '/workspace',
        '--env', 'HOME='+RUNTIME_ENV['HOME'], '--env', 'PATH='+RUNTIME_ENV['PATH'],
        '--env', 'LANG='+RUNTIME_ENV['LANG'], '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,size=64m',
        '--log-driver', 'local', '--log-opt', 'max-size=1m', '--log-opt', 'max-file=1', '--log-opt', 'compress=false',
        '--volume', str(attempt/'worktree')+':/workspace:Z',
        '--volume', str(attempt/'git-mask')+':/workspace/.git:ro,Z']
    if any(m.source == 'scratch' for m in profile.mounts):
        args += ['--volume', str(attempt/'scratch')+':/scratch:Z']
    return args + ['--entrypoint', receipt['argv'][0], profile.image, *receipt['argv'][1:]]


def verify_container(item, receipt, profile):
    host, config = item['HostConfig'], item['Config']
    if environment_digest(config.get('Env')) != receipt.get('environment_sha256'):
        raise WorkerError('Container environment differs from the approved image policy')
    expected = {'/workspace': (receipt['worktree'], True),
                '/workspace/.git': (str(Path(receipt['attempt_path'])/'git-mask'), False)}
    if any(m.source == 'scratch' for m in profile.mounts):
        expected['/scratch'] = (str(Path(receipt['attempt_path'])/'scratch'), True)
    actual = {m['Destination']: (m['Source'], m['RW']) for m in item['Mounts']}
    valid = (actual == expected and all(m['Type'] == 'bind' for m in item['Mounts']) and
        config['User'] == f'{profile.user}:{profile.group}' and host['NetworkMode'] == 'none' and
        host['IpcMode'] == 'private' and host.get('PidMode', '') == '' and
        host.get('UTSMode', '') == '' and host.get('UsernsMode', '') == '' and
        host.get('Init') is True and not host.get('Devices') and
        host.get('Tmpfs') == {'/tmp': 'rw,nosuid,nodev,noexec,size=64m'} and
        not host['Privileged'] and not host.get('Devices') and not host.get('CapAdd') and
        not host.get('PortBindings') and host['ReadonlyRootfs'] and
        'ALL' in host['CapDrop'] and 'no-new-privileges' in host['SecurityOpt'] and
        host['Memory'] == profile.memory_mb * 1024 * 1024 and
        host['MemorySwap'] == host['Memory'] and host['PidsLimit'] == profile.pids_limit and
        host['NanoCpus'] == int(profile.cpus * 1_000_000_000) and
        host['RestartPolicy']['Name'] == 'no' and item['Image'] == receipt['image_id'])
    if not valid:
        raise WorkerError('Created container does not match the validated security plan')


def write_receipt(path, receipt, phase=None, **values):
    if phase:
        receipt['phase'] = phase
        receipt.setdefault('events', []).append({'phase': phase, 'at': stamp()})
    receipt.update(values); receipt['observed_at'] = stamp()
    atomic(path/'worker.json', receipt)


@contextlib.contextmanager
def attempt_lock(path):
    fd = os.open(path/'.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise ControllerActive('Attempt controller is active; cancellation request may be queued') from None
        yield
    finally:
        os.close(fd)


def load_attempt(path):
    path = private_directory(path)
    file = path/'worker.json'
    if file.is_symlink() or file.stat().st_uid != os.getuid() or file.stat().st_mode & 0o077:
        raise WorkerError('Unsafe attempt receipt')
    receipt = json.loads(file.read_text())
    if (receipt['attempt_path'] != str(path) or path.name != receipt['attempt_id'] or
        receipt['worktree'] != str(path/'worktree') or
        receipt['container_name'] != 'swb-'+receipt['attempt_id'] or
        not re.fullmatch(r'[0-9a-f]{32}', receipt['attempt_id'])):
        raise WorkerError('Attempt identity mismatch')
    return path, receipt


def verify_worktree(receipt):
    worktree = Path(receipt['worktree'])
    if worktree.is_symlink(): raise WorkerError('Worktree path changed')
    listing = git(receipt['repository'], 'worktree', 'list', '--porcelain').decode()
    if 'worktree '+str(worktree)+'\n' not in listing:
        raise WorkerError('Worktree ownership no longer matches repository')
    if git(worktree, 'rev-parse', 'HEAD').decode().strip() != receipt['revision']:
        raise WorkerError('Worktree revision changed; preserve for review')


def export_artifacts(docker, receipt, item):
    attempt, worktree = Path(receipt['attempt_path']), Path(receipt['worktree'])
    destination = attempt/'artifacts'; destination.mkdir(mode=0o700, exist_ok=True)
    if destination.is_symlink(): raise WorkerError('Artifact directory changed')
    outputs = []
    def save(name, data):
        if len(data) > MAX_ARTIFACT: raise WorkerError('Artifact too large; worktree retained')
        p = destination/name
        if p.is_symlink(): raise WorkerError('Artifact path changed')
        fd, tmp = tempfile.mkstemp(dir=destination)
        with os.fdopen(fd, 'wb') as stream: stream.write(data)
        os.replace(tmp, p)
        outputs.append({'path': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
    if item and item['State'].get('Status') != 'created':
        save('output.txt', command(docker + ['logs', '--tail', '1000', item['Id']], combined=True))
    if worktree.exists():
        verify_worktree(receipt)
        # Only changed tracked inputs enter the patch. Untracked/ignored files stay
        # in the retained worktree unless explicitly declared as artifacts.
        changed = git(worktree, 'diff', '--name-only', '-z', 'HEAD').split(b'\0')
        total = 0
        for name in changed:
            if not name: continue
            p = worktree / os.fsdecode(name)
            if p.is_symlink() or not p.exists(): continue
            if not p.is_file(): raise WorkerError('Special changed file; preserve for review')
            total += p.stat().st_size
            if total > MAX_ARTIFACT: raise WorkerError('Changed files too large for bounded export')
        save('changes.patch', git(worktree, 'diff', '--binary', '--no-ext-diff', '--no-textconv', 'HEAD'))
        save('status.txt', git(worktree, 'status', '--porcelain', '--untracked-files=all', '--ignored'))
        # Explicit requested files only; source-relative names cannot escape via symlinks.
        for index, name in enumerate(receipt['artifact_paths']):
            p = worktree/name
            if p.resolve() != p.absolute() or not p.is_file():
                raise WorkerError('Declared artifact missing or unsafe; worktree retained')
            if p.stat().st_size > MAX_ARTIFACT: raise WorkerError('Declared artifact too large')
            save('file-'+str(index), p.read_bytes())
            outputs[-1]['source_relative_path'] = name
    save('exit.json', json.dumps({'phase': receipt['phase'], 'exit_code': receipt.get('exit_code'),
        'revision': receipt['revision'], 'image': receipt['profile']['image']}, sort_keys=True).encode())
    atomic(destination/'manifest.json', {'attempt_id': receipt['attempt_id'], 'patch_scope': 'tracked changes only', 'files': outputs})
    return str(destination/'manifest.json')


def finalize(path, receipt, docker, item):
    if item and item['State']['Running']:
        raise WorkerError('Cannot collect or clean a running worker')
    try:
        manifest = receipt.get('artifact_manifest')
        if receipt.get('artifacts') != 'complete':
            manifest = export_artifacts(docker, receipt, item)
        write_receipt(path, receipt, artifact_manifest=manifest, artifacts='complete')
    except (WorkerError, OSError) as exc:
        write_receipt(path, receipt, artifacts='incomplete', cleanup='retained', reason=str(exc))
        # A stopped owned container can be retained for missing logs/artifact recovery.
        return receipt
    if item:
        current = inspect_container(docker, receipt)
        if current:
            if current['State']['Running']: raise WorkerError('Worker state changed before cleanup')
            command(docker + ['rm', current['Id']])
    worktree = path/'worktree'
    retained = []
    if worktree.exists():
        verify_worktree(receipt)
        dirty = git(worktree, 'status', '--porcelain', '--untracked-files=all', '--ignored')
        if dirty: retained.append('worktree_changes')
        else: git(receipt['repository'], 'worktree', 'remove', str(worktree))
    scratch = path/'scratch'
    if scratch.exists():
        if scratch.is_symlink(): raise WorkerError('Scratch path changed')
        if any(scratch.iterdir()): retained.append('scratch_contents')
        else: scratch.rmdir()
    write_receipt(path, receipt, cleanup='retained' if retained else 'complete', retained=retained)
    return receipt


def recover(attempt, sudo=False, cancel=False):
    path, receipt = load_attempt(attempt)
    docker = docker_prefix(sudo)
    with attempt_lock(path):
        if receipt.get('cleanup') == 'complete': return receipt
        try:
            item = inspect_container(docker, receipt)
            expired = receipt.get('deadline', float('inf')) <= time.time()
            if item and item['State']['Running'] and (cancel or expired):
                command(docker + ['stop', '--time', '5', item['Id']], timeout=15)
                item = inspect_container(docker, receipt)
                write_receipt(path, receipt, 'cancelled', reason='operator_cancel' if cancel else 'timeout')
            if item and item['State']['Running']:
                write_receipt(path, receipt, 'running')
                return receipt
            if item:
                phase = receipt['phase'] if receipt['phase'] in {'cancelled', 'start_failed'} else 'exited'
                if item['State'].get('Status') == 'created':
                    phase = 'start_failed'
                write_receipt(path, receipt, phase, exit_code=item['State']['ExitCode'])
            elif receipt['phase'] not in {'exited', 'cancelled', 'start_failed', 'planned', 'preparing'}:
                write_receipt(path, receipt, 'unknown', cleanup='retained', reason='container_absent_without_exit_evidence')
                return receipt
            return finalize(path, receipt, docker, item)
        except (WorkerError, OSError) as exc:
            write_receipt(path, receipt, 'unknown', cleanup='retained', reason=str(exc))
            return receipt


def run_worker(raw, repository, revision, state_root, task, argv, artifact_paths=(), sudo=False, attempt_id=None):
    profile = parse_profile(raw)
    if profile.backend != 'docker': raise WorkerError('Use the native launcher for tmux work')
    if profile.user != os.getuid() or profile.group != os.getgid():
        raise WorkerError('Initial runner requires the host caller numeric user/group')
    if not argv or any(not isinstance(a, str) or not a or '\x00' in a for a in argv):
        raise WorkerError('Worker command must be a nonempty argument vector')
    if not isinstance(task, str) or not task or len(task) > 200:
        raise WorkerError('Provide an explicit bounded task reference')
    for name in artifact_paths:
        if not isinstance(name, str) or Path(name).is_absolute() or any(part in ('..', '.git') for part in Path(name).parts) or '\x00' in name or name in ('', '.'):
            raise WorkerError('Artifact paths must be relative and contained')
    root = private_directory(state_root)
    repo = Path(repository).resolve(strict=True)
    if root == repo or repo in root.parents or root in repo.parents:
        raise WorkerError('Worker state must be outside the repository')
    commit = git(repo, 'rev-parse', '--verify', '--end-of-options', str(revision)+'^{commit}').decode().strip()
    # Configured clean/smudge filters can execute host commands during checkout/export.
    config = git(repo, 'config', '--name-only', '--list').decode().splitlines()
    if any(key.startswith('filter.') for key in config):
        raise WorkerError('Repositories with configured content filters are unsupported initially')
    docker = docker_prefix(sudo)
    capacity = json.loads(command(docker + ['info', '--format', '{{json .}}']))
    if profile.cpus > capacity['NCPU'] or profile.memory_mb * 1024 * 1024 > capacity['MemTotal']:
        raise WorkerError('Requested resources exceed daemon host capacity')
    image = json.loads(command(docker + ['image', 'inspect', profile.image]))[0]
    if image['Config'].get('Volumes'):
        raise WorkerError('Image-declared anonymous volumes are unsupported')
    env_digest = environment_digest(image['Config'].get('Env'), image=True)
    identity = attempt_id or uuid.uuid4().hex
    if not re.fullmatch('[0-9a-f]{32}', identity): raise WorkerError('Invalid attempt ID')
    path = root/identity
    # Repeated IDs must use inspect/recover; never launch a duplicate or overwrite receipts.
    path.mkdir(mode=0o700)
    receipt = {'attempt_id': identity, 'attempt_path': str(path), 'task': task,
        'repository': str(repo), 'revision': commit, 'worktree': str(path/'worktree'),
        'container_name': 'swb-'+identity, 'token': uuid.uuid4().hex, 'container_id': None,
        'image_id': image['Id'], 'environment_sha256': env_digest, 'profile': profile.metadata(), 'argv': list(argv),
        'artifact_paths': list(artifact_paths), 'cleanup': 'pending'}
    with attempt_lock(path):
        write_receipt(path, receipt, 'planned')
        try:
            prepare_execution(raw, path/'execution.json')
            write_receipt(path, receipt, 'preparing')
            git(repo, 'worktree', 'add', '--detach', str(path/'worktree'), commit)
            (path/'scratch').mkdir(mode=0o700)
            (path/'git-mask').write_text('Git administration is host-owned.\n')
            (path/'git-mask').chmod(0o444)
            write_receipt(path, receipt, 'starting')
            container = command(create_argv(docker, receipt, profile)).decode().strip()
            write_receipt(path, receipt, container_id=container)
            item = inspect_container(docker, receipt)
            if not item: raise WorkerError('Created container cannot be observed')
            verify_container(item, receipt, profile)
            write_receipt(path, receipt, deadline=time.time() + profile.timeout_seconds)
            command(docker + ['start', container])
            write_receipt(path, receipt, 'running')
            deadline = time.monotonic() + profile.timeout_seconds
            while True:
                item = inspect_container(docker, receipt)
                if not item: raise WorkerError('Container disappeared without exit evidence')
                if not item['State']['Running']:
                    write_receipt(path, receipt, 'exited', exit_code=item['State']['ExitCode'])
                    break
                if (path/'cancel.request').exists() or time.monotonic() >= deadline:
                    command(docker + ['stop', '--time', '5', container], timeout=15)
                    item = inspect_container(docker, receipt)
                    if not item: raise WorkerError('Exit evidence unavailable after cancellation')
                    write_receipt(path, receipt, 'cancelled', exit_code=item['State']['ExitCode'],
                        reason='operator_cancel' if (path/'cancel.request').exists() else 'timeout')
                    break
                time.sleep(.25)
            return finalize(path, receipt, docker, item)
        except (WorkerError, OSError, KeyboardInterrupt) as exc:
            phase = receipt['phase'] if receipt['phase'] in {'exited', 'cancelled'} else (
                'unknown' if receipt['phase'] == 'running' else 'start_failed')
            write_receipt(path, receipt, phase,
                cleanup='retained', reason='controller_interrupted' if isinstance(exc, KeyboardInterrupt) else str(exc))
    # Recover after releasing the controller lock; an interrupt authorizes stopping this attempt only.
    return recover(path, sudo=sudo, cancel=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sudo', action='store_true', help='Use noninteractive sudo for local Docker only')
    sub = parser.add_subparsers(dest='operation', required=True)
    launch = sub.add_parser('run')
    for name in ('profile', 'repository', 'revision', 'state-root', 'task'):
        launch.add_argument('--'+name, required=True)
    launch.add_argument('--artifact', action='append', default=[])
    launch.add_argument('argv', nargs=argparse.REMAINDER)
    for name in ('inspect', 'recover', 'cancel'):
        sub.add_parser(name).add_argument('attempt', type=Path)
    sub.add_parser('review').add_argument('attempt', type=Path)
    dispose_parser = sub.add_parser('dispose')
    dispose_parser.add_argument('attempt',type=Path)
    dispose_parser.add_argument('--review-sha256',required=True)
    args = parser.parse_args()
    try:
        if args.operation == 'run':
            # A resolved profile is converted back to its supported input shape; no secret values survive.
            profile = load_profile(args.profile)
            raw = profile.metadata()
            argv = args.argv[1:] if args.argv[:1] == ['--'] else args.argv
            result = run_worker(raw, args.repository, args.revision, args.state_root, args.task,
                                argv, args.artifact, args.sudo)
        elif args.operation in ('review','dispose'):
            from .worker_disposition import review, dispose
            result = review(args.attempt,args.sudo) if args.operation == 'review' else dispose(args.attempt,args.review_sha256,args.sudo)
        elif args.operation == 'inspect':
            path, receipt = load_attempt(args.attempt)
            try:
                item = inspect_container(docker_prefix(args.sudo), receipt)
                result = {'receipt': receipt, 'observed_at': stamp(),
                          'runtime': item['State'] if item else None}
            except WorkerError:
                result = {'receipt': receipt, 'observed_at': stamp(), 'runtime': 'unknown'}
        else:
            if args.operation == 'cancel':
                path, _ = load_attempt(args.attempt)
                atomic(path/'cancel.request', {'requested_at': stamp()})
            try:
                result = recover(args.attempt, sudo=args.sudo, cancel=args.operation == 'cancel')
            except ControllerActive:
                if args.operation != 'cancel': raise
                result = {'attempt_path': str(path), 'cancellation': 'requested'}
        print(json.dumps(result, indent=2))
        if args.operation == 'run' and (result['phase'] != 'exited' or result.get('exit_code') != 0 or result.get('artifacts') != 'complete'):
            raise SystemExit(1)
    except (WorkerError, ProfileError, OSError, ValueError) as exc:
        parser.exit(2, 'Worker error: '+str(exc)+'\n')


if __name__ == '__main__':
    main()
