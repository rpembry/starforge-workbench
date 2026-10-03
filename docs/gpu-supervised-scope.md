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

## Miner start gate candidate

The follow-on [isolated request candidate](gpu-isolated-request-candidate.md)
adds a read-only, fail-closed registry check suitable for a reviewed systemd
`ExecCondition=` on the watcher user unit. It prevents a new watcher start
while a durable hold exists or the registry is unavailable. The exact-unit
controller still stops an already-running watcher and verifies exit. The
watcher's lock/display/input policy remains authoritative after restart. No
installed watcher or service is changed by this source slice.

An Ollama request proxy is a separate follow-on. An unproxied direct local
Ollama call can still bypass reservations, and this scope contract does not
prove that an individual request to a long-lived model server completed.
