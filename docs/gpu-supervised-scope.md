# Supervised workload evidence contract

This source-only slice stacks on #199. It does not create a systemd scope,
install a service, inspect a live workload, or change miner behavior.
`SupervisedScopeProbe` validates evidence supplied by a separately reviewed,
trusted local supervisor. There is no default host scope source or peer
resolver. No new privileged access or credential is requested.

An authenticated Unix-socket peer must be bound by that trusted resolver to
one `ScopeBinding`: host boot identity, opaque scope identity, supervisor
generation, cgroup inode, and root PID/start-time generation. The binding is
encoded as the durable lease owner so a restarted adapter can recover the
same identity without trusting a client-supplied scope or PID. The source
must return a fresh observation of the *same* scope generation and all of
its nested process members. The probe checks every member's current
start-time generation and rejects old, missing, malformed, or mismatched
evidence. A boot or cgroup generation change cannot reuse an old binding.

The controller passes its current durable leases to the probe. A KFD PID is
permitted only if it is a current member of one of those supervised scopes.
Overlapping leases may contribute multiple scopes. The controller samples
KFD PIDs, scope generations, KFD PIDs again, and scope generations again;
changes or unknown evidence keep the grant pending. This is bounded
positive attribution, not a static PID allowlist. It does not establish
visibility into GPU contexts omitted by the ROCm KFD probe; host-specific
validation remains required before deployment.

On expiry, the probe returns `ended=True` only for a fresh supervisor
attestation that the *entire* scope has ended with no members. Client or
parent PID death alone is insufficient. A crash, unreadable scope, uncertain
GPU work, or lost binding returns unknown and retains the hold. Restored
leases are already fenced until controller reobservation. These contracts
still need a real supervisor implementation and independent host validation.

## Future watcher hold check

Before a live cutover, the existing idle watcher must check durable hold
state through a narrowly scoped local read interface **before every miner
start** and stop its owned runner when a hold appears. Missing, stale, or
unavailable hold state must prevent a start. Restarting the watcher must not
clear an active hold; its lock/display/input idle rules and any-hour default
remain in force. The watcher must terminate and reap the miner to release
VRAM, rather than pause it with `SIGSTOP`. No installed watcher or service is
changed by this document or source slice.

An Ollama request proxy is a separate follow-on. An unproxied direct local
Ollama call can still bypass reservations, and this scope contract does not
prove that an individual request to a long-lived model server completed.
