# Internal worker execution profiles

Issue #49 implements profile validation and a private metadata handoff in
`starforge_workbench.execution`. It does not launch Docker workers, add a public
schema/API, or change persistent native sessions. The runner in #50 must consume
the resolved profile before launch. See the [backend ADR](adr-docker-worker-backend.md)
and [evaluation gate](docker-worker-evaluation.md).

## Examples and selection

The [native example](../config/execution.tmux.example.yaml) is equivalent to
omitting a profile:

```yaml
backend: tmux
repository_strategy: existing
```

Native validation does not discover Docker, contact a daemon, inspect images or
change the existing command/environment/checkout policy. Docker-only settings on
a native profile fail rather than appearing to be enforced. Existing launcher
commands remain unchanged; these files are not new `ai-workbench` flags or fields
in its persistent-context manifest.

The [Docker example](../config/execution.docker.example.yaml) uses a deliberately
nonexistent example registry/digest. Replace it with an approved, real immutable
image before #50 trials. Validation checks syntax, not availability or image
contents. `toolchain` is an operator label; the image digest identifies the
actual runtime. This example is not an invitation to pull an arbitrary image.

Docker requires explicit non-root user/group, CPU, memory, PID and duration
limits, per-task worktree allocation, and network `none`. Numeric validation is
strict: booleans, strings, nonfinite numbers and out-of-range values fail. The
upper bounds are input sanity limits, not a claim the host has those resources;
#50 must check feasibility and apply limits to the created container.

Mounts describe only allocation roles: `worktree` at `/workspace`, and optionally
`scratch` at `/scratch`, both writable. They cannot name a host path, socket,
home directory, arbitrary container destination, duplicate role or extra mount
option. The runner must bind these roles to verified per-attempt allocations;
profile validation alone cannot prove filesystem containment. In particular,
#50 must mask the host worktree's `.git` pointer as described by the ADR and
validate actual canonical paths, mount overlap and artifact containment.

The first implementation accepts only an empty `secret_refs` list. Its reserved
shape is a list of symbolic names, not paths, URLs, environment assignments or
inline values. Nonempty references fail explicitly because secret delivery is
not implemented. Nothing reads credentials or silently inherits a provider home.
Host/bridge networking, environment injection, privileged mode and arbitrary
Docker flags are likewise unsupported and rejected. Future capability expansion
requires an explicit implementation and review; unknown keys are errors.

## Internal use

```python
from pathlib import Path
from starforge_workbench.execution import load_profile, prepare_execution

native = load_profile()  # No filesystem read or Docker probe.
profile = load_profile(Path("execution.yaml"))
# The controller supplies a new filename inside its already allocated 0700
# attempt directory, and passes the original parsed mapping to prepare_execution.
```

`parse_profile(mapping)` returns an immutable `ExecutionProfile`; `load_profile`
reads YAML using safe loading and delegates to the same validator. An explicitly
missing, empty, malformed or invalid file raises `ProfileError`, never falls back
to native. Errors do not echo values or parser excerpts that might contain secrets.

`prepare_execution(mapping_or_none, metadata_path)` validates first, writes an
exclusive mode-0600 JSON receipt, flushes it, and returns the resolved profile.
No receipt or runtime is created for invalid input. The parent must already be
private, caller-owned and not reached through a symlink alias. Existing files or
symlinks at the receipt path are not replaced. If creation or writing fails,
the caller must not launch; a partial receipt can be inspected as a failed
preparation, never treated as evidence of a running worker.

Receipt shape:

```json
{"execution": {"backend": "tmux", "repository_strategy": "existing", "mounts": []}, "phase": "planned"}
```

Docker receipts additionally include the resolved image/toolchain, numeric limits,
user/group, network and canonical role mounts. They include no credential values,
secret references, environment contents or host allocation paths. Reordering mount
entries produces identical resolved metadata. The receipt is a private internal
handoff; it does not create an API run, heartbeat, accepted action or artifact.
The runner remains responsible for attempt/resource identity and the rest of its
ownership record. This unversioned structure is not a compatibility promise to
external consumers.

#50 must validate the requested configuration before resource creation, consume
the returned profile rather than reread mutable raw input, check host/image/path
prerequisites, record ownership and enforce the ADR's fixed runtime restrictions.
It must never launch natively after a Docker failure. More profile choices should
be added only when supported by the runner; the initial parser intentionally
rejects capabilities that cannot yet be enforced.

## Verification

`uv run pytest tests/test_execution.py -q` covers both examples, no-profile native
behavior, unchanged launcher dispatch, invalid/unsupported settings, secret/error
redaction, canonical metadata and private receipt creation without spawning a
worker. Full repository tests remain required. These checks establish configuration
behavior, not actual container isolation, image usability or provider compatibility.
