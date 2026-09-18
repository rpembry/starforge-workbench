# Initial Docker worker evaluation (#54)

Follow-up: [matched toolchain and disposition results](docker-worker-matched-evaluation.md).
The observations below remain the original run.

**Recommendation: iterate on the basic runner. Keep #51 and #53 gated.**

The synthetic experiment supports the runner's basic safety and correctness. It
does not yet demonstrate the ADR's required benefit from incompatible pinned
toolchains, so it cannot justify expanded credentials or lifecycle integration.
No provider sessions, default backend, or deployed services were changed.

## Measured results

The final measured run contains 48 trials: per backend, ten sequential attempts,
three concurrent pairs, and two repetitions each of invalid startup, cancellation,
controller interruption, and failed artifact export. Each native attempt uses its
own tmux socket, worktree and transient user scope. Docker uses the merged #50
runner. Both receive one CPU, 64 MiB memory, no swap and 32 tasks/processes.

| Rubric | Result |
| --- | --- |
| Minimum Docker boundaries | Passed: effective configuration validation; non-root, no capabilities, no-new-privileges, read-only root/Git mask, CPU/memory/PID limits, only loopback networking, no Docker socket, and inaccessible synthetic host canary. Invalid profiles allocated no resources. Unsafe artifact symlinks were rejected. |
| Reproducibility | All ten Docker sequential attempts passed assertions and produced the same tracked-source patch. |
| Isolation | All three concurrent pairs per backend passed with separate worktrees and incompatible synthetic shell-library APIs. Base checkout unchanged. This does not demonstrate an advantage over native worktrees. |
| Setup/recovery | All final trials terminated; cancellation and interruption recovered. Every resource had ownership records. Repeated cleanup was harmless. No fixture worktrees or containers remained after explicit synthetic teardown. |
| Artifact usefulness | Successful exports had valid hashes; their patches applied to fresh worktrees and passed assertions. Failed exports preserved work and reported the unsafe path. |
| Timing | Nine warm pairs: median additional Docker launch-to-ready time **0.45 s**, maximum **0.45 s**, within the predeclared 5 s/15 s thresholds. |
| Runtime/resources | Fixture runtime was below the 10 ms clock resolution for the median of both backends; this supports only the short-command allowance, not a meaningful speed ratio. Highest recorded cgroup memory peak: **6,488,064 bytes**. CPU usage is recorded per successful trial. |
| Disk/cold preparation | Cached image size **3,855,272 bytes**, below the predeclared 128 MiB budget. Task caches were new, but image download and truly cold OS/image caches were not measured. |
| Operator friction | **Unknown for real work.** The harness automatically discarded only synthetic changes. The ordinary runner correctly retains real edits for review, and a production disposition workflow was not exercised. |

[Machine-readable results](evaluations/docker-workers-54.json) include all trials,
fixed thresholds, image/revision identities, timestamps, timing definitions,
resource observations, patch hashes, failures and the recommendation. Public data
omits local paths and account IDs. Private receipts and original results remain
in the evaluation directory. A first diagnostic run is retained separately; it
exposed a native reporting issue when an interrupted tmux controller had already
removed its transient scope. The final run handles that absent scope explicitly.
No acceptance thresholds changed between runs.

## Interpretation and next experiment

Native used the host GNU shell, while Docker used the digest-pinned Alpine shell.
The same POSIX fixture passed in both, but those are **different toolchains**.
The conflicting shell-library API fixture proves isolated state; it does not
prove that incompatible real toolchains work concurrently without host changes.
Thus timing measurements are descriptive, not a complete equivalence claim.

Before reconsidering #51/#53:

1. Pin the same native and container toolchain and use a real dependency conflict
   or demonstrate a native cold setup/repair step Docker avoids.
2. Measure cold image preparation separately and record the associated operator
   steps; keep current thresholds and retain these original results.
3. Exercise review/export/worktree disposition for ordinary edits without treating
   safe retention as a cleanup failure or silently deleting user changes.

This is an initial #54 result, not approval to expand Docker or deploy it. #54
remains open for that comparable follow-up. #52 remains deferred.

## Reproduction

Use a reviewed, already cached immutable shell image and a new private directory.
The harness needs Git, tmux, systemd user scopes, cgroup v2 and a local Docker
socket. `--sudo` is optional and applies only to authorized noninteractive Docker
access. It never installs services or touches the normal tmux server.

```sh
uv run python -m starforge_workbench.worker_evaluation \
  --root /tmp/new-worker-evaluation \
  --image alpine@sha256:YOUR_REVIEWED_DIGEST --sudo
```

The command writes `results.json` and `report.md`. Limits and thresholds are saved
before the boundary probe; a failed boundary stops further trials. Both backends
use fresh task-local dependency caches. Shared image and operating-system caches
are retained. Native has host networking, but the fixture makes no network
requests; Docker uses `none`. No service fixture or provider credential is used.

Launch-to-ready uses Linux monotonic uptime sampled before controller launch and
inside the fixture; it includes process, scope/container and worktree preparation.
Command time is fixture completion minus readiness, at 10 ms resolution.
Post-command time ends when the controller returns and includes artifact export
and runner cleanup; synthetic patch verification/teardown is additional harness
work. Image size is Docker's reported cached size, not registry transfer size.
Native controller interruption kills the private tmux server; Docker interruption
kills its controller process, then invokes one explicit recovery operation.
These mechanisms differ and are recorded, not equated to provider interruption.
