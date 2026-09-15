"""Internal worker profiles. Validation/metadata only; never launches a worker."""
from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import re

import yaml


class ProfileError(ValueError):
    """Invalid/unsupported configuration; messages never echo supplied values."""


@dataclass(frozen=True)
class Mount:
    source: str  # Allocation role, never a host path.
    target: str
    read_only: bool = False


@dataclass(frozen=True)
class ExecutionProfile:
    backend: str = 'tmux'
    repository_strategy: str = 'existing'
    image: str | None = None
    toolchain: str | None = None
    user: int | None = None
    group: int | None = None
    cpus: float | None = None
    memory_mb: int | None = None
    pids_limit: int | None = None
    timeout_seconds: int | None = None
    network: str | None = None
    mounts: tuple[Mount, ...] = ()

    def metadata(self):
        result = {key: value for key, value in asdict(self).items() if value is not None}
        result['mounts'] = [asdict(mount) for mount in self.mounts]
        return result


IMAGE = re.compile(
    r'(?:[a-z0-9.-]+(?::[0-9]{1,5})?/)?'
    r'[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*'
    r'@sha256:[a-f0-9]{64}'
)
DOCKER_KEYS = {'backend', 'repository_strategy', 'image', 'toolchain', 'user', 'group',
               'cpus', 'memory_mb', 'pids_limit', 'timeout_seconds', 'network', 'mounts',
               'secret_refs'}


def integer(data, key, minimum, maximum):
    value = data.get(key)
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProfileError('Invalid or missing '+key)
    return value


def parse_profile(raw=None):
    if raw is None:
        return ExecutionProfile()
    if not isinstance(raw, dict):
        raise ProfileError('Execution profile must be a mapping')
    backend = raw.get('backend', 'tmux')
    if backend == 'tmux':
        if set(raw) - {'backend', 'repository_strategy'}:
            raise ProfileError('Native profile does not support Docker settings')
        if raw.get('repository_strategy', 'existing') != 'existing':
            raise ProfileError('Native profile must preserve the existing repository strategy')
        return ExecutionProfile()
    if backend != 'docker':
        raise ProfileError('Unsupported execution backend')
    if set(raw) - DOCKER_KEYS:
        raise ProfileError('Unsupported execution setting')
    if raw.get('repository_strategy') != 'per-task-worktree':
        raise ProfileError('Docker requires a per-task worktree')
    image = raw.get('image')
    if not isinstance(image, str) or len(image) > 512 or not IMAGE.fullmatch(image):
        raise ProfileError('Docker image must be a repository pinned by sha256 digest')
    toolchain = raw.get('toolchain')
    if not isinstance(toolchain, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._+-]{0,63}', toolchain):
        raise ProfileError('Invalid or missing toolchain label')
    user = integer(raw, 'user', 1, 2147483647)
    group = integer(raw, 'group', 1, 2147483647)
    cpus = raw.get('cpus')
    if type(cpus) not in (int, float) or not 0 < cpus <= 256 or not math.isfinite(cpus):
        raise ProfileError('Invalid or missing cpus')
    memory = integer(raw, 'memory_mb', 16, 1048576)
    pids = integer(raw, 'pids_limit', 1, 65536)
    timeout = integer(raw, 'timeout_seconds', 1, 86400)
    if raw.get('network') != 'none':
        raise ProfileError('Only isolated network policy none is supported initially')
    refs = raw.get('secret_refs', [])
    if not isinstance(refs, list) or any(not isinstance(ref, str) or not re.fullmatch(
            r'[a-zA-Z][a-zA-Z0-9_-]{0,63}', ref) for ref in refs):
        raise ProfileError('Secret references must be symbolic names, never values or paths')
    if refs:
        raise ProfileError('Secret delivery is not supported by the initial runner')
    mounts = raw.get('mounts')
    if not isinstance(mounts, list) or not 1 <= len(mounts) <= 2:
        raise ProfileError('Declare worktree and optional scratch mounts')
    resolved = []
    seen = set()
    for mount in mounts:
        if not isinstance(mount, dict) or set(mount) != {'source', 'target', 'read_only'}:
            raise ProfileError('Invalid mount declaration')
        source = mount['source']
        if not isinstance(source, str) or source not in {'worktree', 'scratch'} or source in seen:
            raise ProfileError('Mounts must use unique worktree/scratch allocation roles')
        target = '/workspace' if source == 'worktree' else '/scratch'
        if mount['target'] != target or type(mount['read_only']) is not bool:
            raise ProfileError('Unsupported mount target or access mode')
        if mount['read_only']:
            raise ProfileError('Task worktree/scratch must be writable')
        seen.add(source)
        resolved.append(Mount(source, target))
    if 'worktree' not in seen:
        raise ProfileError('Docker requires a worktree mount')
    return ExecutionProfile(backend='docker', repository_strategy='per-task-worktree',
        image=image, toolchain=toolchain, user=user, group=group, cpus=float(cpus),
        memory_mb=memory, pids_limit=pids, timeout_seconds=timeout, network='none',
        mounts=tuple(sorted(resolved, key=lambda mount: mount.source)))


def load_profile(path=None):
    """Omission preserves native behavior; explicit bad files never fall back."""
    if path is None:
        return parse_profile()
    try:
        with Path(path).open() as stream:
            raw = yaml.safe_load(stream)
    except (OSError, yaml.YAMLError):
        raise ProfileError('Cannot read execution profile') from None
    if raw is None:
        raise ProfileError('Explicit execution profile must not be empty')
    return parse_profile(raw)


def prepare_execution(raw, metadata_path):
    """Validate before creating an exclusive private worker metadata receipt.

    The future runner must consume this resolved profile, bind allocation roles to
    verified per-task paths, and enforce runtime controls before it starts work.
    This receipt is not evidence that any runtime was created or started.
    """
    profile = parse_profile(raw)
    path = Path(metadata_path)
    parent = path.parent
    # Parent must already be an allocated private directory, with no symlink aliases.
    if parent.resolve() != parent.absolute() or not parent.is_dir():
        raise ProfileError('Worker metadata requires a real allocated directory')
    info = parent.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ProfileError('Worker metadata directory must be private and owned by the caller')
    payload = json.dumps({'execution': profile.metadata(), 'phase': 'planned'}, sort_keys=True)+'\n'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return profile
