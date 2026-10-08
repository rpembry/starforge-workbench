"""Offline dedicated-host-identity preparation; no privilege changes or launcher."""
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import stat

from .vm_sandbox import _open_without_symlinks


class ProfileError(RuntimeError):
    def __init__(self):
        super().__init__('Dedicated diagnostic profile unavailable')


def _id(value):
    return type(value) is int and 0 < value < 2**31


def _path(value):
    # Rendering restriction, not a confinement proof. Actual opening walks
    # dirfds with O_NOFOLLOW; the sandbox must have an independent mount boundary.
    return (isinstance(value, Path) and value.is_absolute() and '..' not in value.parts
            and re.fullmatch(r'/[A-Za-z0-9_./-]+', str(value))
            and value.parts[1] not in ('proc', 'sys', 'dev'))


@dataclass(frozen=True, slots=True)
class FileGrant:
    source: Path = field(repr=False)
    destination: Path

    def __post_init__(self):
        if (not _path(self.source) or not _path(self.destination)
                or self.destination not in {Path('/diagnostics/capacity.json'),
                                           Path('/diagnostics/connectivity.json'),
                                           Path('/diagnostics/os-runtime.json')}):
            raise ProfileError()


@dataclass(frozen=True, slots=True)
class DedicatedIdentityProfile:
    controller_uid: int
    executor_uid: int
    executor_gid: int
    publisher_uid: int
    protected: tuple[Path, ...] = field(repr=False)
    files: tuple[FileGrant, ...] = field(repr=False)

    def __post_init__(self):
        if (not all(_id(value) for value in (self.controller_uid, self.executor_uid,
                                             self.executor_gid, self.publisher_uid))
                or self.executor_uid in (self.controller_uid, self.publisher_uid)
                or type(self.protected) is not tuple or not self.protected
                or any(not _path(p) for p in self.protected)
                or type(self.files) is not tuple or not 1 <= len(self.files) <= 3
                or any(type(grant) is not FileGrant for grant in self.files)
                or len({g.source for g in self.files}) != len(self.files)
                or len({g.destination for g in self.files}) != len(self.files)):
            raise ProfileError()
        # Reject contradictory configured grants; this is only configuration
        # validation, never evidence of symlink/procfd/descriptor confinement.
        for grant in self.files:
            if any(grant.source == p or p in grant.source.parents or grant.source in p.parents
                   for p in self.protected):
                raise ProfileError()


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    uids: tuple[int, int, int]
    gids: tuple[int, int, int]
    groups: tuple[int, ...]
    initial_user_namespace: bool
    no_new_privileges: bool
    capabilities_clear: bool
    inherited_fds_closed: bool


def inspect_runtime_identity():
    """Read only process identity metadata; never inspect descriptor contents."""
    try:
        status = Path('/proc/self/status').read_text()
        fields = dict(line.split(':', 1) for line in status.splitlines() if ':' in line)
        caps = all(int(fields[name].strip(), 16) == 0
                   for name in ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb'))
        mapping = Path('/proc/self/uid_map').read_text().split()
        # Namespace-root mapping to the operator is not host identity separation.
        initial = mapping == ['0', '0', '4294967295']
        # Listing may include the transient directory FD. Verify each candidate
        # after enumeration instead of treating its stale entry as inherited.
        candidates = tuple(int(name) for name in os.listdir('/proc/self/fd') if int(name) > 2)
        inherited = []
        for fd in candidates:
            try:
                os.fstat(fd)
                inherited.append(fd)
            except OSError:
                pass
        return RuntimeIdentity(os.getresuid(), os.getresgid(), tuple(sorted(os.getgroups())),
                               initial, fields['NoNewPrivs'].strip() == '1', caps, not inherited)
    except Exception:
        raise ProfileError() from None


def verify_runtime_identity(profile, observation):
    """Trusted supervisor observation before grants; this does not activate a profile."""
    if (type(profile) is not DedicatedIdentityProfile or type(observation) is not RuntimeIdentity
            or type(observation.uids) is not tuple or type(observation.gids) is not tuple
            or any(type(v) is not int for v in (*observation.uids, *observation.gids))
            or observation.uids != (profile.executor_uid,) * 3
            or observation.gids != (profile.executor_gid,) * 3
            or type(observation.groups) is not tuple
            or observation.groups not in ((), (profile.executor_gid,))
            or any(type(v) is not int for v in observation.groups)
            or any(type(v) is not bool or not v for v in
                   (observation.initial_user_namespace, observation.no_new_privileges,
                    observation.capabilities_clear, observation.inherited_fds_closed))):
        raise ProfileError()


def open_exact_grants(profile):
    """Pin publisher-owned regular files after identity preflight. Caller owns FDs.

    Group read is the only executor grant: publisher:executor_gid mode0640,
    one link, no symlink components. Files are data, never executables. No
    caller/MCP source/destination/FD argument exists. No process is launched.
    """
    verify_runtime_identity(profile, inspect_runtime_identity())
    opened = []
    try:
        for grant in profile.files:
            fd = _open_without_symlinks(grant.source, nonblocking=True)
            opened.append(fd)
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != profile.publisher_uid or info.st_gid != profile.executor_gid
                    or stat.S_IMODE(info.st_mode) != 0o640):
                raise ProfileError()
            # Retain only these intentional read-only descriptors. Future
            # launcher must pass exactly these, close_fds=True, clearenv, and
            # bind each through --ro-bind-fd without mounting source parents.
            os.set_inheritable(fd, False)
        return tuple(opened)
    except Exception:
        for fd in opened:
            try:
                os.close(fd)
            except OSError:
                pass
        raise ProfileError() from None


def operator_plan(profile):
    """Inspectable proposal only; none of these requirements are installed."""
    if type(profile) is not DedicatedIdentityProfile:
        raise ProfileError()
    return {
        'schema_version': 1, 'state': 'requires_operator_deployment_and_live_preflight',
        'host_identity': {'controller_uid': profile.controller_uid, 'executor_uid': profile.executor_uid,
                          'executor_gid': profile.executor_gid, 'publisher_uid': profile.publisher_uid},
        'file_permissions': {'owner': profile.publisher_uid, 'group': profile.executor_gid, 'mode': '0640'},
        'read_only_files': [{'source': str(g.source), 'destination': str(g.destination)} for g in profile.files],
        'protected_roots': [str(p) for p in profile.protected],
        'required_runtime': ['initial_host_identity_preflight', 'empty_capability_sets', 'no_new_privileges',
                             'no_supplementary_groups', 'close_unlisted_fds', 'clear_environment',
                             'private_mount_pid_network_namespaces', 'fd_pinned_read_only_file_mounts',
                             'no_host_home_procfd_or_directory_mounts', 'child_inherits_confinement',
                             'bounded_cgroup_and_io_supervision'],
        'privileged_helper': 'none', 'activation': 'unsupported',
    }
