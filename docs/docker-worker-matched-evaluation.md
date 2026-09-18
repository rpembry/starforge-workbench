# Matched-toolchain follow-up (#54)

**Recommendation: iterate on the basic runner; do not start #51/#53.**

The matching-toolchain and cold task-preparation gaps are now measured. The
edit-disposition experiment found a concrete operator-friction failure: accepting
an exported patch does not retire the original dirty worktree, and preserving
all untracked/ignored outputs requires an extra archival step. Safe retention is
correct; the missing integrated review/archive/disposition workflow is the gap.

## Review correction and replacement evidence

The review identified unsupported pass claims in the earlier results: cleanup
used a controller-duration check of 40 seconds instead of the declared 30-second
cleanup gate, disk accounting omitted downloaded and extracted toolchain data,
and the local review lock did not cover peer subprocess finalization. The
historical JSON below remains unchanged, but its cleanup and disk gate labels
are superseded by [the corrected 48-trial run](evaluations/docker-workers-54-reviewed.json).

The corrected run passed with a maximum cleanup upper bound of **1.06 seconds**
and total retained runtime/cache storage of **9,173,400 bytes**: 3,855,272 cached
image bytes + 3,846,087 fresh image-store bytes + 1,472,041 extracted binaries and
wrappers. The unchanged limits are 30 seconds and 128 MiB. Median/max warm
startup overhead was 0.44/0.47 seconds. Operator friction still failed.

Every paired controller must finish before any host review or teardown starts.
A failed peer finalizer defers review/teardown for the pair, preserving receipts.
This barrier excludes host review overlapping a still-finalizing peer; it does
not establish the cause of the old Git exit 128 or claim all possible Git races
are fixed. Worker execution and worker-owned finalization remain concurrent.

Cleanup measurement ends after review, recovery, synthetic teardown and final
inventory. It starts at the fixture's pre-exit completion sample, or at trial
launch for failure scenarios, adding one 10 ms clock tick for uptime truncation.
This is a conservative **upper bound** on exit-to-cleanup duration; it includes
barrier waiting and may reject long-running failure trials conservatively. A
missing, negative, nonfinite or over-30-second bound cannot pass. The original
controller-only duration is retained separately as `controller_elapsed_s`.

## Historical evidence (before review corrections)

| Check | Follow-up result |
| --- | --- |
| Same toolchain | Native and container BusyBox and musl binaries matched SHA-256 exactly. Native uses private loader/applet wrappers, with no host package or library changes. |
| Cold task preparation | Fresh registry download into new storage plus private native extraction stayed below the unchanged 300-second limit. Exact times and stored bytes appear in the result JSON. Shared Docker and OS caches were retained, as permitted by the protocol; this is not an empty-daemon or cold-page-cache benchmark. |
| Sequential/concurrent/failure samples | Final run: all 48 trials completed; reproducibility, runtime boundary, shell-library isolation, artifacts and resource cleanup checks passed. |
| Warm launch cost | Median Docker overhead 0.44 seconds; maximum 0.45 seconds, below the unchanged 5-second/15-second limits. Median command time remained below the 10 ms clock resolution. |
| Review and preservation | The actual runner's exported patch was verified, accepted into a separate synthetic Git branch, and all five worktree files were preserved in a verified archive, including ignored/untracked outputs. Original checkout unchanged. |
| Operator friction | **Failed.** Normal review/acceptance was followed by two extra cleanup actions: archive/verify remaining outputs, then explicitly remove the reviewed worktree. Repeated normal recovery still retained it after acceptance. |
| Concrete workload benefit | Still not demonstrated for this small task. Both native and Docker isolate its state correctly. Matching a container image on the host adds setup, but this alone is not evidence that a real workload needs Docker. |

The [matched result](evaluations/docker-workers-54-matched.json) includes 48 trial
records and the separate disposition experiment. The [previous failed matched
run](evaluations/docker-workers-54-matched-failed.json) remains published: one
concurrent host-side artifact-review/teardown operation returned Git exit 128,
leaving two explicitly identified synthetic worktrees. The original local review lock was incomplete, and one successful rerun
did not establish a race fix. The barrier described above replaces that claim;
worker execution remains concurrent. The original error did not include enough
Git diagnostics to prove its precise cause. Those retained worktrees were then
cleaned after recording the evidence. No acceptance threshold was changed.

The [original different-shell run](docker-worker-evaluation-results.md) is also
retained. Its observations have not been replaced by the more favorable rerun.
No evaluation containers or worktrees remain. Accepted synthetic commits and
verified archives remain in private evaluation storage.

## Repeatable follow-up command

```sh
uv run python -m starforge_workbench.worker_evaluation \
  --root /tmp/new-matched-evaluation \
  --image alpine@sha256:YOUR_REVIEWED_DIGEST --sudo --matched-toolchain
```

This additional mode supports Linux x86_64 Alpine images containing BusyBox and
musl. It uses `skopeo` to fetch the pinned image into a new directory, verifies
layer hashes, and copies only two regular binary members; it never extracts an
image filesystem wholesale. Toolchain bytes must match an actual restricted
Docker probe before comparison trials run. Native applet wrappers execute the
same BusyBox through its private musl loader. The measured pinned image is in
the machine-readable record. This mode uses the already cached Docker image;
registry download time is measured independently in isolated storage.

The disposition exercise uses only its own committed synthetic repository. It
is **not** a new production command that can delete user worktrees. It archives
and hashes every fixture file except Git administration, rejects symlinks and
special files, verifies the archive, accepts the patch into a named synthetic
branch, and then explicitly cleans its owned fixture allocations. The count of
extra cleanup actions reflects this demonstrated workflow; automation inside the
experiment does not pretend the normal runner offers that workflow already.

## Next bounded change

Add a review-aware disposition operation that verifies current ownership and a
complete durable archive before removing a reviewed worktree. It must preserve
untracked/ignored outputs, refuse changed or ambiguous evidence, and be safe to
repeat. Exercise that operation through the ordinary task flow and rerun the
zero-manual-cleanup gate. Separately, select a representative real toolchain
conflict before claiming a reason to expand Docker. Keep native as the default;
provider credentials, richer lifecycle integration and service fixtures remain
gated/deferred.
