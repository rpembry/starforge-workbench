# Opt-in Docker task attempts

Issue #50 adds `starforge_workbench.docker_worker`, a separate host-controlled
runner. Persistent native tmux launch and attach remain unchanged. It runs an
explicit command in a new detached Git worktree and a uniquely labelled container,
then returns a private JSON receipt with status, ownership and artifact paths.
It does not create API tasks, declare task completion, or authenticate a provider.
The next evaluation gate is [#54](docker-worker-evaluation.md).

## Prerequisites and use

Use a trusted local repository without configured Git content filters, a locally
cached, reviewed digest-pinned image, and an existing private state directory
outside that repository. This slice requires Linux, Git, Docker's local socket,
and numeric profile user/group matching the non-root caller. Resource requests
must fit the daemon host's CPU count and total memory; this does not reserve
capacity against other workloads. Images declaring volumes are rejected. The
runner never pulls images or falls back to native execution.

Copy [the example profile](../config/execution.docker.example.yaml) outside Git,
replace its illustrative image with an actual approved digest, set the caller's
numeric user/group, and choose limits appropriate for that image. For example:

```sh
mkdir -m 700 /tmp/worker-attempts
uv run python -m starforge_workbench.docker_worker run \
  --profile /path/to/private/execution.yaml \
  --repository /path/to/repository --revision HEAD \
  --state-root /tmp/worker-attempts --task example-task \
  --artifact result.txt -- sh -c 'your-test-command > result.txt'
```

`--sudo`, before the subcommand, selects noninteractive sudo for Docker commands
only, where the host administrator has authorized it. The host controller and
Git remain unprivileged. Do not run the whole controller as root. Keep state on
persistent local storage when recovery across reboots is needed; `/tmp` above is
only an example for disposable trials.

Commands and their arguments are stored in the private receipt; do not place
credentials in them. Network access, credential delivery and provider-home
mounting are unsupported. This runner currently suits offline tools and images
with their dependencies already installed. A provider's authentication workflow
needs separate evaluation and implementation.

## Isolation and evidence

Before starting, the runner records ownership, resolves the commit and image,
creates the worktree, and inspects the actual container against its plan. Each
attempt has its own worktree, temporary filesystem, and optional `/scratch`.
It publishes no ports and uses network `none`. Container execution is non-root,
with all capabilities dropped, no-new-privileges, a read-only image filesystem,
and explicit CPU, memory, PID and time limits. It mounts neither the Docker
socket nor the host home. SELinux private relabels apply only to attempt mounts.

The host owns Git. A read-only mask covers the worktree's `.git` pointer inside
the container; the repository's administrative directory is never mounted.
In-container Git is deliberately unavailable in this first slice. An image
must work with the specified arbitrary UID/GID and without writable root paths.

Receipts distinguish planned, preparing, starting, running, exited, cancelled,
start_failed and unknown. A zero exit code describes the command, not completion
of a Workbench task. An unavailable daemon or missing exit evidence yields unknown
visibility and retained ownership. No existing provider session is changed.

After stopping, artifacts include bounded combined stdout/stderr, a binary patch
of **tracked** changes, Git status (including untracked and ignored paths), exit
metadata, and explicitly requested files. The manifest records sizes and SHA-256
hashes. Logs rotate at 1 MiB and export only the final 1,000 lines. Each output and
the aggregate changed tracked inputs are limited to 8 MiB; subprocess output and
probe time are bounded. Requested file paths must be relative, regular files,
outside `.git`, and cannot traverse symlinks. Outputs are evidence, never executed
by collection. Untracked files are not silently included in the patch.

## Cleanup, cancellation and recovery

```sh
uv run python -m starforge_workbench.docker_worker inspect /path/to/attempt
uv run python -m starforge_workbench.docker_worker cancel /path/to/attempt
uv run python -m starforge_workbench.docker_worker recover /path/to/attempt
```

Use the same Docker access option as for launch. Cancellation queues an atomic
request if the controller is active; otherwise recovery stops the exact owned
container. The controller checks cancellation and timeout while running; recovery
also checks the persisted deadline. A killed controller does not leave a new
background watchdog: invoke recover after an interruption, especially if the
worker is still running. Docker restart policy is `no`.

Cleanup verifies the attempt labels, random ownership token, container identity
and worktree registration. It removes only an owned stopped container and a clean
worktree after artifact export. Dirty worktrees, ignored files, scratch contents,
failed exports and ambiguous ownership remain explicitly tracked for review.
The runner never force-deletes edits. Review retained work and remove it manually
only when no longer needed. Receipts and exported artifacts remain for audit.
Repeated recovery preserves completed artifact exports and does not relaunch a
worker; reusing an attempt ID for launch is rejected.

## Tests

Ordinary tests never require Docker. Live tests are opt-in and use synthetic Git
repositories; provide an already cached shell-capable image by immutable digest:

```sh
WB_TEST_DOCKER=1 WB_TEST_DOCKER_IMAGE=alpine@sha256:YOUR_REVIEWED_DIGEST \
  uv run pytest -q tests/test_docker_worker.py
```

Set `WB_TEST_DOCKER_SUDO=1` for the authorized noninteractive-sudo test environment.
The checks cover concurrent edits, restricted runtime, retained artifacts,
idempotent cleanup, timeout, cancellation, unavailable commands and rejected
creation. They establish basic runner behavior, not provider compatibility or the
cost/benefit decision in #54.

## Review and archive retained work

A completed command can leave changes that normal cleanup intentionally retains.
Use the explicit review/dispose flow after reviewing the patch and deciding how
to accept its source changes:

```sh
uv run python -m starforge_workbench.docker_worker review /path/to/attempt
# Inspect the returned review_manifest, existing artifacts, and retained work.
uv run python -m starforge_workbench.docker_worker dispose /path/to/attempt \
  --review-sha256 EXACT_REVIEW_SHA256
```

Use `--sudo` before the subcommand if required for Docker observation. The review
hash pins a byte-and-mode inventory; it is not authentication or proof that a
person reviewed it. Disposal is an explicit operation, not implied by exit zero,
patch acceptance elsewhere, an attention alert, or `recover`.

The operation requires complete artifacts, a terminal attempt, no remaining owned
container, the controller lock, matching repository/worktree ownership and an
unchanged review snapshot. It inventories tracked, untracked and ignored files,
empty directories, scratch, and exported artifacts. This initial version refuses
staged changes, changed HEAD, symlinks, hardlinks, special files, more than 10,000
entries or more than 128 MiB of payload. Unsupported work stays available for
manual review; it is never silently omitted from an archive.

Disposal moves the original worktree and scratch directories into the attempt's
private `archive/`, flushes files/directories, rechecks the inventory, then removes
only the missing old worktree registration using Git without `--force` or a global
prune. Artifacts retain their existing paths. The private journal is written before
moves, making retries safe after interrupted moves or unregistration. Original
file bytes remain in the archive; there is no automatic archive purge. The
archive's old `.git` pointer is historical metadata, not a usable checkout—restore
source files into a fresh worktree rather than resuming Git inside the archive.

Do not edit the attempt concurrently with review/disposal. The controller lock
coordinates Workbench commands, not arbitrary editors. Detected changes or a
reappearing source path stop the operation and preserve both locations. If failure
occurs before moves, create a fresh review after resolving changes. After moves,
retry the same approved token; changed archives or ambiguous state require manual
inspection. `dispose` never repairs that ambiguity by deleting either copy.

## Environment policy (#51, first increment)

Image environment defaults are untrusted configuration. Before allocating an
attempt, the runner rejects unknown names, duplicate names, malformed entries,
and values outside the fixed policy. Each entry is bounded to 256 characters.
The accepted names and values are:

| Name | Accepted image value | Effective value |
| --- | --- | --- |
| `PATH` | `/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`, optionally prefixed with `/usr/local/bin:` by official Python images | Standard path without the duplicate prefix, explicitly supplied |
| `HOME` | `/tmp` or `/root` | `/tmp`, explicitly supplied |
| `LANG` | `C`, `C.UTF-8`, or `en_US.UTF-8` | `C.UTF-8`, explicitly supplied |
| `PYTHON_VERSION` | Bounded numeric major.minor.patch, optional a/b/rc suffix | Image value |
| `PYTHON_SHA256` | 64 hexadecimal characters | Image value |
| `GPG_KEY` | 40 hexadecimal characters (public key fingerprint) | Image value |

Missing image entries are allowed; the runner always supplies PATH/HOME/LANG.
Python metadata supports the official images used in the evaluation, without
allowing arbitrary environment entries. Image variables such as tokens,
`LD_PRELOAD`, proxy settings, and alternate executable search paths fail closed.
Use an image with approved defaults; this implementation has no bypass flag.

The created container must have exactly the expected effective environment before
it can start. The receipt stores a SHA-256 fingerprint of that environment,
not its values. Error messages do not echo supplied names or values. Host
credentials are not forwarded to Docker with `--env` or `--env-file`.

This is configuration enforcement, not secret detection or output redaction.
Do not place secrets in images, commands, task labels, committed repositories,
logs, or artifacts: those channels are not made safe by an environment allowlist.
A digest pins image identity, not image trust. Nonempty `secret_refs` remain
rejected, and no provider credentials are delivered. Task-scoped credential
injection/redaction and audited exceptions remain outstanding #51 work. Native
launchers, network `none`, role-only mounts and the existing isolation controls
are unchanged.
