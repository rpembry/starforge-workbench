# Python workload benefit decision (#54)

**Recommendation: stop Docker expansion for this workload. Keep native execution
as the default, retain the opt-in prototype, and do not start #51/#53.**

Docker runs the selected incompatible Python workloads successfully. The existing
native runtimes also run them concurrently without installation, dependency repair
or a default-version change. This experiment therefore does not establish an
incremental Docker benefit that justifies more credentials or lifecycle work.
This is a decision about the tested workstation/workload, not a claim that Docker
has no value elsewhere. It does not evaluate a requirement to run untrusted code;
the prototype’s boundary protections are distinct from this runtime-setup comparison.

## Representative capability probe

The legacy case performs packaging-version comparisons using `distutils.version`,
which belongs to the Python 3.11 standard library. The modern case generates typing
metadata with `typing.ReadOnly` and Python 3.14's `annotationlib`. The two cases
were placed in one committed synthetic repository and executed in separate worktrees.
Each correct runtime passed its assertions; switching runtimes caused the expected
specific missing-API error. Unrelated command failures cannot count as proof of a
language-version conflict.

The native baseline uses the already installed Python 3.11 and 3.14 interpreters,
private tmux sockets and transient user scopes. Docker uses official Alpine Python
images pinned by digest. Both receive one CPU, 64 MiB RAM, no swap, 32 processes
and a ten-second task limit. Docker has network `none`; the native fixture performs
no network operations. No dependencies or service fixtures are installed.

The final invocation uses `-I -S -B`: isolate Python environment, exclude site
compatibility packages, and suppress bytecode writes. Excluding compatibility
packages matters: a host shim can otherwise supply `distutils` on newer Python.
The conflict is between the actual standard-library contracts, not an assertion
that an existing shim could not bridge it.

## Evidence and boundaries

- Three concurrent pairs per backend: **12 successful capability executions**.
- One wrong-runtime check per case/backend: **four expected missing-API failures**.
- All 16 attempts cleaned up their worktrees; base checkout, selected native
  interpreter hashes and default-Python selection were unchanged.
- Both Docker images together occupy **38,204,900 reported cached bytes**, below
  the unchanged 128 MiB image/cache budget. No separate image download store or
  extracted native runtime was created in this experiment.
- Images were absent before the initial preparation checks; first pull calls took
  about 6.63 and 5.15 seconds, with shared base layers/OS caches retained. The final
  repeat records both images as cached. This is not an empty-daemon benchmark.
- The two native runtimes required **zero installations or dependency repairs**.
  Explicitly selecting an existing executable sufficed. Docker did not avoid a
  measured native setup problem.

[Final machine-readable evidence](evaluations/python-toolchain-benefit.json)
contains every attempt, versions, image identities, resource plan, hashes, cache
state and decision. [The first run](evaluations/python-toolchain-benefit-first.json)
is preserved separately; it used `-I -S` before the explicit `-B` repeat.

This supplements the earlier [48-trial evaluation](docker-worker-matched-evaluation.md).
It is a capability experiment, not another editing benchmark or a performance
comparison: native uses glibc, the Alpine images use musl, and measured launch
elapsed times must not be presented as a controlled speed comparison. The native legacy interpreter is 3.11.15 while the container is 3.11.16;
both modern interpreters are 3.14.7. Those patch/platform differences are explicit
limitations on timing equivalence, not failures of the tested API contracts. The earlier matched BusyBox/musl
trials remain the timing/boundary evidence; prior reports and failures are retained.

The reviewed-work disposition change in PR #80 has been approved and awaits merge.
This experiment starts from merged main and neither merges nor deploys that change.
Its archive/disposition safety should be judged separately from whether Docker is
worth expanding.

## Reproduction

This command explicitly pulls the specified immutable images during preparation;
the worker itself still uses `--pull never`. Supply approved digests and existing
native interpreter paths. The output directory must be new and private.

```sh
uv run python -m starforge_workbench.python_toolchain_evaluation \
  --root /tmp/new-python-capability-evaluation \
  --legacy-python /path/to/python3.11 \
  --modern-python /path/to/python3.14 \
  --legacy-image python@sha256:REVIEWED_LEGACY_DIGEST \
  --modern-image python@sha256:REVIEWED_MODERN_DIGEST --sudo
```

`--sudo` only selects authorized noninteractive local Docker access. Existing
provider sessions, the default tmux server, installed services and host interpreter
selection are not changed. Raw logs and ownership records stay in private storage.

Revisit expansion only for a named workload that demonstrates a concrete unmet
native requirement—for example, a system-library dependency that cannot be
satisfied by the existing isolated runtimes—with an explicit resource budget and
owner acceptance of the resulting recommendation. Do not start #51/#53 solely
because the prototype passed its tests. #52 service fixtures remain deferred.
