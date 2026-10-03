# Exact user-unit controller slice

This source-only slice extends the GPU reservation adapter with an optional
`ExactUnitController`. It does not install, enable, start, or stop a real
service. The exact user service name and runtime paths belong in reviewed
private configuration outside Git. Tests use only a synthetic service.

The controller targets one configured user-systemd unit. Before STOP, it captures the unit invocation, main PID, cgroup, and
PID/start-time pairs across the unit cgroup and nested child cgroups. Only
the captured main process receives a pidfd-bound TERM after a fresh invocation
check; a replacement invocation is never stopped by unit name. The existing
watcher is expected to stop its children through its normal TERM trap. If it
does not, the lease remains pending. There is no process-name search,
arbitrary PID signal, `SIGSTOP`, or global GPU reset. A second observation can report
absence only after the unit is inactive, its captured cgroup is empty, every
captured process generation has exited, and none of the captured PIDs retains
a KFD GPU context. A changed invocation blocks a grant even if the previous
processes are gone. An unrelated desktop or model process may still hold VRAM.
Missing systemd, `/proc`, cgroup, or ROCm evidence yields UNKNOWN. A captured
generation held only in memory is lost on controller restart; an already
inactive unit then stays UNKNOWN and cannot grant a lease or restart without
operator recovery. Persisted capture and unattended recovery require a
separate review before production use.

The controller reports its idle eligibility as true because the existing
user-unit watcher remains the authority for display, lock, and input idle
conditions after START. An optional time-window policy can still restrict
eligibility, but the default remains any hour. Higher-priority workload
end proof comes from an injected supervised-scope probe; there is no default
probe that equates parent PID death with job completion.

The backend requires Linux pidfd support through the host libc. If that entry
point is unavailable, exact stop fails closed. The provided source is not a
claim that an untested host's service trap or process group cleanup works.

## Ollama follow-on

This slice does not intercept Ollama. A later local lease-aware proxy could
wrap a selected request path, hold the reservation through confirmed request
completion, and conservatively retain it after an uncertain disconnect. A
direct call to Ollama's unproxied local port would bypass that path. Covering
all callers would require a separately reviewed endpoint cutover and checks
for stream cancellation, loaded-model state, and restart behavior. No model
pull, benchmark, prompt logging, or service reconfiguration is included here.

Before any live cutover, review exact unit ownership, systemd permissions,
private adapter storage, target process and GPU evidence on the host, one
controller at a time, rollback, and a nonprivate A/B check. No root or device
permission expansion is presumed by this implementation.
