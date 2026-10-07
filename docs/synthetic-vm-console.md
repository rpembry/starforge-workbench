# Disposable synthetic VM console adapter

This opt-in infrastructure slice follows #279 and the independently reviewed
synthetic seam in #280. It supplies a **fresh synthetic guest console adapter**,
not a production SSH transport or an interface to existing/private guests.
`wb-vm-mcp` still starts with no guest registrations or transport. No user,
ACL, sudo rule, credential, firewall, port forward or persistent service is
created by importing these modules or running ordinary pytest.

## Operator-owned configuration and confinement

`SyntheticVMPolicy` accepts absolute private base/overlay paths, protected host
directories and bounded CPU/RAM/lifetime values. This configuration is local
trusted operator code, never an MCP argument. Paths must be distinct, with no
symlink component or `..`; images are regular files owned by the current user
with no group/world access. The caller verifies official image provenance and
checksum before use and creates a new overlay for each attempt. Images are not
committed to Git. The base must have no external backing chain.

`sandbox_plan` opens checked source inodes using directory FDs and no symlink
following. Bubblewrap mounts only read-only `/usr`, the read-only base image and
one writable overlay file. It binds the open FDs rather than resolving image
paths again; renaming/replacing a host path cannot swap the granted inode. The
mount namespace starts with an empty root and private proc/dev/tmp, so host
home, credentials, host `/etc` and surrounding temporary directories are absent.
Protected-directory/mount overlaps reject configuration; those checks prevent
contradictory grants, while the OS namespace is the isolation mechanism.

User, PID, network, IPC and other supported namespaces are required, with nested
user namespaces disabled, capabilities dropped, environment cleared and a new
session. Child commands inherit confinement. QEMU is a fixed executable with
TCG, bounded vCPU/guest RAM, no default devices/config, no NIC, display, monitor,
QMP passthrough or exposed ports. QEMU's seccomp sandbox rejects privilege
elevation, spawning and resource-control operations. There is no silent fallback
when sandbox setup fails.

The bootstrap is trusted operator code outside the sandbox. Namespace UID mapping
does **not** create a separate host account or implement the local-Qwen/cloud
Linux identity/ACL separation requested in #281/#282. Dedicated host identities
and production deployment remain separate authorization/review gates. Arbitrary
Python running as the host operator is outside this boundary.

## Hard resource and lifecycle gate

Before launching, `SyntheticConsoleTransport` requires an active cgroup v2 with
at most 1536 MiB physical memory, zero swap, one CPU quota and 128 tasks. It reads
and verifies controls without changing them. The operator must create an owned
transient user unit with `RuntimeMaxSec=120`, `TimeoutStopSec=3` and
`KillMode=control-group`; no shared service is modified. Guest defaults are one
vCPU and 768 MiB RAM. Guest lifetime is limited to 120 seconds by a local timer;
the unit provides an independent lifetime/descendant cleanup boundary.

The QEMU process also has a 90-second CPU limit, a 64 MiB writable-file limit and
core dumps disabled. The image has a bounded virtual disk (the evidenced fixture
uses 3 GiB); the overlay file cap bounds growth. Physical cgroup RAM is used
instead of an overly small virtual-address-space limit. The caller must use a
single-threaded local bootstrap because process limits are set with `preexec_fn`.
This is not a general-purpose in-server VM launcher.

`close()` cancels the timer, terminates the owned process group, escalates to
SIGKILL after three seconds if needed and reaps the process. Bubblewrap's
die-with-parent behavior and the cgroup contain descendants. A failed boot,
timeout or collection-cap violation stops the guest. Never reuse its overlay
after failure. Only one guest lifecycle is owned by this adapter instance.

## Minimal shared diagnostic contract

The adapter implements the same interface from #280:

```python
from pathlib import Path
from workbench.vm_diagnostics import Diagnostic, DiagnosticService, GuestRegistration
from workbench.vm_sandbox import SyntheticVMPolicy
from workbench.vm_synthetic import SyntheticConsoleTransport

guest = GuestRegistration('g_' + '3' * 32, 'synthetic-console', frozenset(Diagnostic))
policy = SyntheticVMPolicy(
    Path('/tmp/synthetic-vm/base.qcow2'), Path('/tmp/synthetic-vm/overlay.qcow2'),
    protected_directories=(Path('/home'), Path('/etc')),
)
# Execute only in the operator-created, verified bounded transient unit.
with SyntheticConsoleTransport(guest, policy) as transport:
    service = DiagnosticService(registrations=(guest,), transport=transport)
    local_result = service.execute(guest.guest_id, 'os_runtime')
```

The fresh official Debian nocloud fixture provides a passwordless local root
console. This adapter sends only fixed commands for `connectivity`, `os_runtime`
and `capacity`, reading exact `/proc/uptime` and `/proc/meminfo` fields. It does not
take a shell command, path, host or privileged helper from the caller. The root
console is suitable only for a disposable synthetic guest; it is no approval or
authentication scheme for a real machine. Connectivity means a functioning
diagnostic console, not network reachability.

Direct calls recheck exact registration, operation permission and timeout/cap
bounds. Command collection gets at most five seconds and 4096 payload bytes;
small fixed framing overhead is separately bounded. Whole-line random markers
prevent echoed shell command text or terminal control prefixes from becoming a
result. Stderr counts toward collection bounds but cannot supply a result frame.
Raw boot/serial/error bytes are discarded, not logged or published by the adapter.
The domain layer still independently validates exact output schemas and denies
screenshots/content exports. A malicious guest can supply misleading valid
values or covert channels; this is not a correctness or DLP guarantee.

Local batch/release work (#281/#282, PR #284) can consume this versioned diagnostic
result without controlling the guest. Keep result/detail local; automatically
release only the separately operator-approved `diagnostic.status.v1` status
schema. Free-form findings or diagnostic fields require exact-content/destination
review by the outbound broker. VM execution authority does not approve release.
Broker/Qwen integration is a separate test gate, not proven by this adapter smoke.

## Evidence and remaining gates

The author smoke used the official Debian image
`https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-nocloud-amd64.qcow2`
and its HTTPS `SHA512SUMS`, verified before use. Matching a checksum from the
same official HTTPS source is an integrity check, not an independent signature
verification. Image bytes: 424345600; virtual disk: 3221225472. SHA512:

```text
d586dd6eb454373c8f2f50ab4c423620c013320e22a5869a01c9fba4d9c2829dd644e3521e88421bbb43b23a995899356bdbfbee6a7e00db2833e7a211830219
```

QEMU 10.2.2 and bubblewrap 0.12.0 were exercised. A harmless OS probe found host
home, shadow and surrounding temporary paths absent; its child inherited that
result. Active cgroup controls read memory.max=1610612736, swap.max=0,
cpu.max=`100000 100000`, pids.max=128. One fresh guest returned all three domain
schemas and denied screenshot export; cleanup reaped it in about 29 seconds.
This is author evidence at the candidate build, not independent acceptance or
combined cloud/local inference integration.

Normal tests use fake image bytes, filesystem grants, cgroup files and console
frames. They cover injection, symlinks, inode substitution, contradictory mount
grants, immutable limits, absent/excessive cgroup bounds, direct operation denial,
framing/byte caps and stderr injection. Independent exact-build sandbox/guest QA,
combined broker/Qwen synthetic execution, production identity/authorization,
exception semantics and live private-system access remain unapproved/unproven.
No private disks or systems were used in this smoke.
