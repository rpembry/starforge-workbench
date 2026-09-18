# Basic Docker worker evaluation gate

Design prerequisite: [Docker backend ADR](adr-docker-worker-backend.md), #48.
Execution and measured results belong to #54 after #49/#50. This is a pre-build
rubric, not a claim that trials have run or a requirement to improve every task.

## Trial protocol

Use a committed synthetic repository task: edit a small library and run a fixed
assertion suite with a pinned toolchain. Include conflicting dependency versions
in two concurrent attempts to exercise a benefit of runtime isolation. Supply a
deterministic fixture worker, so model nondeterminism, provider costs and secrets
do not obscure runner behavior. This establishes runner utility only; real AI
provider/authentication compatibility requires a separately authorized follow-up.

Run native tmux and Docker against the same base commit, task inputs, assertions,
resource budget and artifact expectations. Give native trials separate worktrees
too, so Git isolation alone is not presented as Docker's unique benefit. Record
native toolchain versions and Docker image digest, cache state and any allowed
network differences. Failed/incomparable trials remain in the results. Do not
change the daily launcher default or touch existing sessions.

Minimum sample: 10 sequential trials per backend (one cold preparation plus
nine warm trials), three concurrent pairs per backend, and two repetitions per
backend of each failure scenario: invalid startup, cancellation, controller
interruption after resource creation, and failed artifact export. Cold means no
task-specific prepared runtime/cache; isolate trial caches without deleting the
operator's shared caches. Record image download/build time separately from launch
latency. Declare the image, limits and test inputs before running; do not tune
pass thresholds after seeing results.

## Measurable rubric

| Dimension | Evidence | Pass condition |
| --- | --- | --- |
| Reproducibility | Base commit, toolchain/image identity, normalized patch and assertion result for every sequential trial | 10/10 Docker trials produce the expected patch and pass the same assertions; warm and cold agree. Exclude timestamps only through documented normalization, never hide differing source/results |
| Worktree/runtime isolation | Before/after source hashes, ownership handles, dependency versions and concurrent-pair results | All three Docker pairs complete correctly with distinct worktrees/containers; no edits reach the base checkout or the other worker; dependency conflicts do not leak between attempts; no undeclared ports or shared writable caches |
| Setup and cleanup | Creation/exit/cleanup receipts and final resource inventory for success and injected failures | Every trial terminates or reports retained/unknown state within its declared timeout; zero untracked containers/worktrees; successful artifact export permits container cleanup within 30 seconds of exit; dirty or unexported work remains explicitly owned and recoverable; a second cleanup is harmless |
| Runtime cost | Monotonic cold preparation, warm launch-to-ready, command runtime and cleanup duration; CPU/RSS/disk observations | For nine warm trials, median Docker launch overhead over native is at most 5 seconds and max at most 15 seconds; median command runtime is at most 1.25× native, with a 2-second absolute allowance for short commands. Cold preparation completes within 5 minutes; record retained image/cache size and require it to fit the predeclared local disk budget |
| Artifact usefulness | Hashes, patch, test summary, exit status, attempt/commit/image identity, collection errors | All successful trials produce a complete manifest and a patch that applies to a fresh copy of the base commit and passes assertions; failed trials preserve available diagnostics with explicit missing-artifact reasons; no artifact path escapes its declared root |
| Operator friction | Prompt count, required commands/manual steps and failures | After one-time explicit setup, each ordinary trial needs at most one task launch and no manual cleanup, permission repair or container lookup; at most one explicit recovery operation per injected failure; failures name the attempt, cause/evidence and next action |

Host load and the chosen resource budget must be recorded alongside timing.
A resource-budget breach is a failed result, not a reason to silently increase
limits. Record the native baseline honestly, including setup burden and any
failures. The comparison should answer which task class benefits, not assume
Docker must win.

## Mandatory boundary checks

Before judging convenience or speed, attempt access to synthetic canaries outside
the worktree/scratch mounts; inspect the effective container configuration; test
non-root execution, privilege/capability restrictions, resource limits, no host
socket/home/Git-administration mounts, default network denial, and artifact path
containment. Test rejection of invalid profiles before resource creation.

All checks must pass. Use synthetic canaries and approved fixtures, never real
credentials. No test may rely on receiving host-Docker access inside the worker.
A daemon smoke test does not substitute for these checks. A boundary failure
blocks real workload trials until fixed and retested; do not defer it to #51.

## Result record and decision

#54 should emit a machine-readable JSON result plus a short operator report.
Each trial records backend, attempt ID, scenario, source revision, toolchain/image
identity, declared limits/cache/network conditions, start/end and phase durations,
exit/assertion results, artifact hashes, cleanup disposition and retained resource
references. Public exports replace private paths with synthetic identifiers.
Aggregate output records sample counts, all failures, metric definitions and
thresholds, each pass/fail/unknown result, and the recommendation with evidence.
This is a harness output contract, not a stable public API schema.

Choose one outcome explicitly:

- **Stop:** material boundary/isolation failures remain unresolved, or Docker
  shows no reproducible task benefit sufficient to justify its additional cost.
  Keep native execution; document what was learned. Do not open #51–#53 as an
  automatic consequence of completing the prototype.
- **Iterate on the basic runner:** evidence is incomplete or a bounded fix could
  address a reliability, cost or friction failure. Name the failed metric, fix
  and rerun scope; keep expansion blocked. Missing evidence cannot count as pass.
- **Proceed:** every mandatory check and rubric threshold passes, and the report
  demonstrates at least one concrete benefit: incompatible toolchains succeed
  concurrently without modifying the host toolchain, or cold reproducibility
  avoids a documented native setup/repair step. Name the workloads for which
  Docker is worth selecting and the remaining limits. Ask the owner to accept
  the recommendation before starting conditional #51/#53 work.

#52 remains deferred even after “proceed” unless a specific service-fixture need
is demonstrated. Threshold changes require an explicit recorded decision and a
new comparable run; retain the original results. An accepted design, a merged
runner and a favorable evaluation are separate milestones.

## Initial evidence

The [initial #54 report](docker-worker-evaluation-results.md) records 48 trials
and an iterate recommendation. Toolchain comparability, cold preparation and
production operator friction remain open; conditional expansion is not approved.

The [matched-toolchain follow-up](docker-worker-matched-evaluation.md) measures
cold task preparation and edit disposition; the latter fails the operator-friction
gate. The recommendation remains iterate, with a specific bounded next change.

Review correction: older cleanup/disk pass labels lacked complete measurements.
Use the [corrected gate evidence](docker-worker-matched-evaluation.md#review-correction-and-replacement-evidence);
original observations remain preserved.

## Workload benefit decision

The [Python capability comparison](docker-python-benefit.md) found both backends
satisfy the tested incompatible standard-library workloads. It recommends stopping
expansion for this workload, retaining native defaults and keeping #51/#53 gated.
