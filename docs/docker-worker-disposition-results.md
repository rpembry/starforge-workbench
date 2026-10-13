# Reviewed-work disposition result (#54)

The archive/dispose operation closes the measured manual-cleanup gap for the
supported offline fixture. Recommendation remains **iterate**: a concrete workload
benefit over native worktrees still has not been demonstrated, so #51/#53 remain
gated. This result does not authorize provider credentials or lifecycle expansion.

The new [ordinary review/dispose flow](docker-workers.md#review-and-archive-retained-work)
was exercised through the public CLI after a real synthetic patch was reviewed
and accepted into a separate branch. Exact content preservation and an identical
second disposal result were verified. It required one explicit snapshot approval,
then **zero subsequent archival or cleanup commands**. Review and approval are
still human workflow actions; the experiment does not label them as automatic.

The [machine-readable result](evaluations/docker-workers-54-disposition.json)
contains 48 matched-toolchain trials and the separate disposition exercise:

- All trial boundaries, reproducibility, isolation, artifact, disk and cleanup
  checks passed with the existing thresholds.
- Warm launch overhead: median 0.43 seconds, maximum 0.46 seconds.
- Public `dispose` completed in approximately 0.14 seconds for this small fixture.
- Archived originals matched every reviewed entry. The source checkout was
  unchanged, and no registered fixture worktree remained.
- The archive, accepted synthetic commit, receipts and exported artifacts remain
  available; no user data was discarded to obtain the cleanup result.

Reproduce with `--matched-toolchain --reviewed-disposition` on the evaluation
command. Earlier failed/manual-disposition observations are preserved. The new
operation is deliberately limited: no staged changes, special files, symlinks or
hardlinks, with bounded payload and entry counts. The safety tests cover changed
content, wrong tokens, scratch changes, unsafe files, reappearing source paths,
and interruptions before and after Git unregistration.
