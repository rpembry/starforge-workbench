"""Operator-owned synthetic QEMU sandbox plan, not a live guest registry.

No CLI/MCP surface accepts these paths. The OS mount/network/PID namespaces
enforce isolation; overlap validation only prevents contradictory grants.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
import os
from pathlib import Path
import stat


@dataclass(frozen=True, slots=True)
class SyntheticVMPolicy:
    base_image: Path = field(repr=False)
    overlay: Path = field(repr=False)
    protected_directories: tuple[Path, ...] = field(default=(), repr=False)
    memory_mib: int = 768
    cpus: int = 1
    lifetime_seconds: int = 120

    def __post_init__(self):
        paths = (self.base_image, self.overlay)
        if (type(self.protected_directories) is not tuple
                or any(not isinstance(p, Path) or not p.is_absolute()
                       or '..' in p.parts for p in (*paths, *self.protected_directories))
                or self.base_image == self.overlay
                or type(self.memory_mib) is not int or not 128 <= self.memory_mib <= 1024
                or type(self.cpus) is not int or not 1 <= self.cpus <= 2
                or type(self.lifetime_seconds) is not int or not 1 <= self.lifetime_seconds <= 120):
            raise ValueError('Invalid synthetic VM policy')


@dataclass(frozen=True, slots=True)
class SandboxPlan:
    argv: tuple[str, ...] = field(repr=False)
    pass_fds: tuple[int, ...] = field(repr=False)
    lifetime_seconds: int


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _open_without_symlinks(path: Path, *, writable=False, nonblocking=False):
    """Open from / using dirfds, never following a symlink component.

    FD mounts bind the checked inode, rather than resolving the host pathname
    again when bubblewrap starts. Returned FD belongs to the enclosing plan.
    """
    current = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for component in path.parts[1:-1]:
            new = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                          dir_fd=current)
            os.close(current)
            current = new
        flags = os.O_RDWR if writable else os.O_RDONLY
        if nonblocking:
            flags |= os.O_NONBLOCK
        return os.open(path.name, flags | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=current)
    finally:
        os.close(current)


@contextmanager
def sandbox_plan(policy: SyntheticVMPolicy):
    """Yield one FD-bound, fixed-command plan; caller MUST bound lifecycle.

    Caller uses Popen(argv, pass_fds=plan.pass_fds, close_fds=True,
    start_new_session=True, env={}, stdin/stdout/stderr=PIPE), stops/reaps it
    within lifetime_seconds and never forwards serial output without the
    DiagnosticService schema/release boundary. No process is launched here.
    """
    if type(policy) is not SyntheticVMPolicy:
        raise ValueError('Invalid synthetic VM policy')
    mounts = ((Path('/usr'), '/usr', False),
              (policy.base_image, '/guest/base.qcow2', False),
              (policy.overlay, '/guest/overlay.qcow2', True))
    fds = []
    try:
        if any(_overlaps(source, protected) for source, _, _ in mounts
               for protected in policy.protected_directories):
            raise ValueError('Invalid sandbox mount grant')
        for source, _, writable in mounts:
            fd = _open_without_symlinks(source, writable=writable)
            fds.append(fd)
            info = os.fstat(fd)
            if source == Path('/usr'):
                if not stat.S_ISDIR(info.st_mode):
                    raise ValueError('Invalid sandbox runtime')
            elif (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                  or info.st_mode & 0o077):
                raise ValueError('Invalid synthetic VM image')
        argv = ['/usr/bin/bwrap', '--unshare-all', '--unshare-user', '--disable-userns', '--new-session',
                '--die-with-parent', '--cap-drop', 'ALL', '--clearenv',
                '--setenv', 'PATH', '/usr/bin', '--setenv', 'HOME', '/nonexistent',
                '--symlink', 'usr/bin', '/bin', '--symlink', 'usr/lib', '/lib',
                '--symlink', 'usr/lib64', '/lib64', '--proc', '/proc', '--dev', '/dev',
                '--tmpfs', '/tmp', '--dir', '/guest']
        for fd, (_, destination, writable) in zip(fds, mounts):
            argv += ['--bind-fd' if writable else '--ro-bind-fd', str(fd), destination]
        argv += ['--chdir', '/guest', '--', '/usr/bin/qemu-system-x86_64',
                 '-machine', 'q35,accel=tcg,dump-guest-core=off', '-cpu', 'max',
                 '-smp', str(policy.cpus), '-m', str(policy.memory_mib),
                 '-nodefaults', '-no-user-config', '-nic', 'none', '-display', 'none',
                 '-monitor', 'none', '-serial', 'stdio', '-no-reboot',
                 '-sandbox', 'on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny',
                 '-drive', 'file=/guest/overlay.qcow2,format=qcow2,if=virtio']
        yield SandboxPlan(tuple(argv), tuple(fds), policy.lifetime_seconds)
    except (OSError, ValueError):
        raise ValueError('Synthetic VM sandbox unavailable') from None
    finally:
        for fd in fds:
            os.close(fd)


def diagnostic_script(operation: str) -> str:
    """Exact diagnostic script for a fresh synthetic guest's local console.

    No caller argument is interpolated. This is not a generic remote shell
    interface. Future transports can map the same Diagnostic enum contract.
    """
    scripts = {
        'connectivity': "printf '{\"reachable\":true}\\n'",
        'os_runtime': (
            "python3 -c 'import json,os; "
            "a=os.uname().machine; "
            "u=int(float(open(\"/proc/uptime\").read().split()[0])); "
            "print(json.dumps(dict(kernel_family=\"linux\", "
            "architecture=a if a in (\"x86_64\",\"aarch64\") else \"other\", "
            "uptime_seconds=u)))'"
        ),
        'capacity': (
            "python3 -c 'import json,os; "
            "m=dict(l.split(\":\",1) for l in open(\"/proc/meminfo\")); "
            "print(json.dumps(dict(cpu_count=os.cpu_count(), "
            "memory_total_bytes=int(m[\"MemTotal\"].split()[0])*1024, "
            "memory_available_bytes=int(m[\"MemAvailable\"].split()[0])*1024)))'"
        ),
    }
    if type(operation) is not str or operation not in scripts:
        raise ValueError('Unsupported synthetic diagnostic')
    return scripts[operation]
