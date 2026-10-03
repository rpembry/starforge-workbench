# Host supervisor journal slice (#176)

`coordinator.supervisor.Supervisor` is an independent host boundary. It needs
only a private state directory, clock, and host-owned runtime adapter. The
coordinator database can be absent while `tick()` enforces persisted deadlines
and orphan policy or `owner_stop()` fences the coordinator and stops an exact
attempt. A service wrapper must run the tick loop independently of the
coordinator and authenticate its owner-only local stop endpoint.

The journal records one active controller lease and monotonically increasing
generation, immutable attempt/operation IDs, a plan hash, runtime identity,
sticky cancellation, original deadline, and orphan deadline. Calls are
serialized across service processes around the authority check and runtime
mutation. A duplicate launch operation returns its journal record and never
calls launch again. If a launch response is lost, reconciliation checks the
exact attempt and runtime identity. It does not create a replacement. Owner
stop fences the current generation before exact runtime stop. An interrupted
stop reports unknown until observation confirms termination.

The default `strict` policy stops at lease expiry plus configured grace, capped
by the original runtime deadline. Explicit `trusted_local` continuation stops
at the earlier original deadline or its persisted orphan budget. Neither
budget is refreshed by a coordinator reconnection. A backward clock step
blocks starts and causes the watchdog to stop owned attempts conservatively.

The runtime adapter contract is in the class docstring. `DockerRuntime` now
implements the approved **scratch plus ordinary command** path. It resolves
symbolic profile/workspace references from administrator configuration, uses
the existing runner's digest-pinned image/environment and restricted create
verification, and checks exact labels/token/ID, plan hash, and incarnation on
every inspect and stop. A runtime mismatch is unknown and is never adopted or
removed blindly. `collect()` exports bounded logs, exit metadata, and declared
regular files after positive stop; the workspace and container remain for
review. No legacy receipt is adopted automatically. The existing Git-worktree
runner remains a separate compatibility path, unchanged by this slice.

This slice contains the journal and fake-Docker adapter tests, **not** an
independently running service/owner authentication, Git-worktree coordinator
adapter, or protocol-aware worker mount. It is therefore not ready for a live
coordinator release. Host death suspends enforcement until a supervisor
service restarts and reconciles. No service installation or live Docker claim
is made by these tests.
