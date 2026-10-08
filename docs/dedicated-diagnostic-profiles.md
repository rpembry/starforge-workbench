# Dedicated diagnostic identity: offline preparation

This separate slice prepares the next deployment contract after the accepted
synthetic VM adapter. It does not alter the accepted PR heads, activate a service,
create users/groups, change ACLs/sudo/network access, boot a VM, invoke a model,
or register an MCP tool. It is not live confinement acceptance.

The controller, trusted diagnostic publisher and executor have distinct roles.
The executor must have a nonroot host UID different from both controller and
publisher. Controller and publisher may share an identity in this proposal;
that is not isolation between those two roles. A user namespace mapping UID0
back to the controller does not satisfy the host identity boundary. Profiles
are immutable operator configuration. No caller selects a host path, file FD,
service, command or privilege helper through MCP.

## Inspectable configuration

```python
from pathlib import Path
from workbench.vm_identity import DedicatedIdentityProfile, FileGrant, operator_plan

profile = DedicatedIdentityProfile(
    controller_uid=21001, executor_uid=21002, executor_gid=21002,
    publisher_uid=21001,
    protected=(Path('/srv/synthetic-private'),),
    files=(FileGrant(Path('/srv/synthetic-published/capacity.json'),
                     Path('/diagnostics/capacity.json')),),
)
proposal = operator_plan(profile)
```

These are synthetic IDs/paths, not a deployment instruction or real inventory.
`operator_plan` returns an inspectable dictionary with identity, exact mounts,
file permission requirements and unfinished runtime requirements. Its state is
`requires_operator_deployment_and_live_preflight`; activation is unsupported.
Its paths/IDs can themselves be sensitive. Keep real plans outside Git and the
automatic outbound channel.

`inspect_runtime_identity` reads only process UID/GID, user namespace mapping,
capability/no-new-privileges fields and descriptor presence. It never reads FD
contents or protected files. `verify_runtime_identity` rejects wrong real,
effective or saved IDs; extra groups; a mapped user namespace; nonempty capability
sets; missing no-new-privileges; or retained descriptors beyond stdio.
Only `EBADF` after descriptor enumeration proves a stale, closed entry. Other
descriptor inspection errors, including access denial or I/O failure, fail closed
with the fixed profile error and cannot authorize file grants.
`open_exact_grants` always collects this runtime observation itself, before
opening grants. Caller-supplied observations cannot activate grants. The pure
verification function is useful for synthetic tests; it is not an authenticated
remote attestation protocol. Descriptor enumeration assumes a single-threaded,
trusted bootstrap. A malicious supervisor can lie or change its own process.

## Exact data grants

Files must be publisher-owned, executor-group-readable mode0640, regular, and
have one hard link. The opener walks each path component with `O_NOFOLLOW`, pins
the resulting inode, opens read-only/nonblocking to refuse special files without
waiting for a FIFO writer, and marks descriptors noninheritable. It accepts only
the destinations `/diagnostics/capacity.json`, `/diagnostics/connectivity.json`
and `/diagnostics/os-runtime.json`, with at most one grant per destination/source.
An opening/validation failure closes already opened descriptors and returns a
fixed error. The caller owns successful descriptors and must close them.

Path formatting and protected-root overlap checks reject contradictory proposals
and ambiguous renderer inputs. **They do not prove confinement.** Symlink walks,
regular-file metadata and pinned descriptors provide narrower concrete guarantees.
The trusted publisher remains able to alter an opened inode; mode/inode checks do
not authenticate content or promise immutable snapshots. Local storage availability
and supervised I/O deadlines are separate deployment requirements.

`numeric_file` releases only the existing exact diagnostic JSON schemas, at most
4096 input bytes, with duplicate/extra keys rejected. Malformed bytes and errors
never return file contents. This contract has no file-reading or transport entry
point. A future transport must bind operation to its exact destination and use a
bounded supervised reader; no arbitrary file or shell read is supplied here.

## Bounded log preparation

`LogWindow` is an immutable, operator-selected exact service and interval:
maximum300 seconds,100 lines,8192 bytes. The service name cannot contain command
options or renderer control characters. `journal_argv` returns a fixed argv tuple
for a future producer. It does not execute journalctl. `--lines` alone does not cap
bytes or wall time; a future collector must kill/reap on deadline or collection-cap
failure and must never log partial raw output.

`log_counts` consumes only normalized JSON-line fixtures with exactly service,
integer timestamp, integer priority0..7 and message. Each service/timestamp must
match the configured window. Message content is limited to1024 UTF-8 bytes per
record and discarded. Byte/line limits, duplicate keys, extra fields, invalid
types, wrong services and out-of-window records fail closed. The result contains
only record count and eight priority counts; no message, service/path or timestamp
is returned. This slice supplies no journal normalization/collection adapter and
no syslog/messages path access. Counts themselves may be sensitive and are not
automatically authorized for release. Existing outbound review rules still apply.

## Exact permissions and live acceptance still needed

An authorized operator must review/provision the dedicated executor UID/GID and
its service launcher, without granting general sudo, host groups or broad journal
membership. Protected directories must be owned by another identity and deny the
executor traversal/read through modes and all applicable ACLs. Exact publisher
directories must permit only necessary traversal; published files need the stated
owner/group/mode. The publisher must never be able to write application code,
executables or other executor mounts. A trusted bounded producer may need narrowly
scoped service-log read permission; executor membership in systemd-journal/adm or
whole `/var/log` mounts are not part of this proposal. No helper is requested here.
If a privileged helper later proves necessary, its fixed operation/arguments,
authentication and failure behavior need a separate review.

The trusted launcher must set the host identity, clear groups/environment, drop
all capabilities, set no-new-privileges, close all unlisted FDs, and run identity
preflight before grants. Then it must build a private mount/PID/network namespace
with only approved runtime mounts and exact FD-pinned read-only data mounts.
Do not mount source parents, protected roots, host home, host proc/PID views,
host sockets or inherited directory FDs. Intentional grant FDs must be the only
`pass_fds`, with `close_fds=True`. Child processes must inherit the mount/identity
boundary and remain within supervised cgroup and I/O limits. No fallback may
run under the controller or outside the sandbox when any required facility fails.

Before activation, independently test the actual deployed identity and ACLs,
namespace layout, direct and child access to synthetic protected canaries,
symlink aliases, `/proc/self/fd` and host-PID proc paths, inherited descriptors,
published inode changes, bounded log producer failure/cleanup and approved mounts.
The tests in this slice exercise actual nofollow/read-only FD behavior and harmless
child FD hygiene plus synthetic identity/log observations. They do **not** create
the deployment, run a sandboxed child under another host UID, or prove OS mount/ACL
confinement. No production/private target scope is authorized by this preparation.
