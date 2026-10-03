# Isolated synthetic GPU request candidate

This is a **source and fake-tested candidate**, stacked on the draft scope
contract. Nothing here installs a user unit, changes the idle watcher, changes
an Ollama service or tunnel, or runs an inference. The first live use is a
separate opt-in commissioning step.

## One-request sequence

1. An exact, disabled user-systemd test service starts a small worker in its
   own cgroup. It does no GPU work yet. `SystemdRequestScopeSource` binds its
   Unix-socket peer credentials to that service's invocation, cgroup inode,
   root process generation, and recursively sampled members.
2. The worker durably acquires a 240-second lease. The miner start gate reads
   the same private SQLite registry. An optional reviewed `ExecCondition=` on
   the existing miner unit returns 0 only when the registry is valid and has
   no leases; active holds and unknown state return 1. It gates a *new* unit
   start, including restart after failure. The existing exact-unit controller
   still stops the already-running watcher and waits for miner cgroup and GPU
   context exit before granting the lease.
3. Only after a confirmed grant does the worker launch a separate `ollama
   serve` child on a non-default loopback port. It checks `/api/tags` for an
   already installed model, sends one fixed synthetic `/api/generate` request
   with an eight-token limit and `keep_alive=0`, and never calls pull/create.
   Loss of a grant or adapter visibility stops the child.
4. The worker stops the child and sends `finish`. This marks the lease stale
   and expired but **does not release the hold**. The adapter may clear it only
   after its trusted supervisor reports the entire exact cgroup ended with
   no members. If teardown, registry, systemd, or KFD evidence is unknown,
   mining stays blocked pending operator recovery.

`gpu_start_gate.py` is a read-only registry check; the code does not install
its `ExecCondition=` drop-in. A private drop-in would use the installed Python
module and an absolute private registry path, for example:

```ini
[Service]
ExecCondition=/path/to/python -m workbench.gpu_start_gate --registry /private/registry.sqlite
```

The exact path and unit are local configuration and must not enter Git. The
condition is checked at service activation, not continuously. The adapter's
captured-generation stop covers a watcher that was already running when a
lease was acquired. The existing watcher keeps its lock, display, input-idle,
and any-hour policy after the hold clears. A scoped peer's old `release`
operation is deliberately treated as `finish`; it cannot skip scope exit.

The `ExecCondition=` result is a **one-time** check, so the candidate depends
on the user-systemd activation state being observable throughout the gap
between that check and `ExecStart`. The controller now refuses to grant while
the unit is `activating`, while a start `Job` is pending, or when that job
property is unavailable or unrecognized. If acquisition commits before a
new condition check, the durable hold makes that check skip startup. Fake
interleaving tests cover both orders, but do not establish the real host's
timing. The systemd documentation says `ExecCondition=` runs during the
[activation transition](https://github.com/systemd/systemd/blob/main/man/systemd.service.xml);
the exact `ActiveState`/`Job` observations still need a controlled host check.

## Commissioning gate and rollback

Before any live test, review the exact private unit and drop-in, registry
path, socket permissions, user-systemd cgroup topology, KFD visibility,
installed-model cache path, and a free loopback port. Verify on this host that
systemd retains enough invocation evidence after the test service exits for
`ended=True`; if it does not, the lease intentionally remains and this
candidate must be revised before inference. Verify the miner's captured
generation can be recovered or plan an explicit operator recovery after an
adapter restart. No arbitrary PID signaling or registry deletion is a
recovery method.

Before enabling a grant-capable adapter, verify with an inert test unit that
`ActiveState` stays `activating` or a pending `Job` stays visible after a
successful `ExecCondition=` check until the watcher has started or the job
has ended. Also verify the local `systemctl show -p Job` format parses as
expected. If the host can report `inactive` with no job in that gap, a one-time
condition is insufficient: keep this path disabled and add a serialized
watcher launch gate before any Ollama inference. The synthetic tests cannot
substitute for this host-specific activation check.

### Inert activation probe for the approved commissioning window

These commands are a **future test plan**, not a performed test. They create
only a temporary user unit in systemd's
[runtime user-unit directory](https://github.com/systemd/systemd/blob/main/man/systemd.unit.xml).
The unit runs shell markers and waits; it does not reference the miner,
adapter, Ollama, or GPU. Run them only after approval to touch the user
manager. Keep the same shell open so the variables remain available.

```sh
probe_root=$(mktemp -d "${XDG_RUNTIME_DIR:?}/gpu-gate-probe.XXXXXX")
probe_name="$(basename "$probe_root").service"
probe_unit="${XDG_RUNTIME_DIR}/systemd/user/${probe_name}"
mkdir -p "${XDG_RUNTIME_DIR}/systemd/user"
test ! -e "$probe_unit" || exit 1
cat > "$probe_root/condition" <<'EOF'
#!/bin/sh
touch "$(dirname "$0")/condition_passed"
EOF
cat > "$probe_root/pre" <<'EOF'
#!/bin/sh
probe_dir=$(dirname "$0")
touch "$probe_dir/pre_entered"
while test ! -e "$probe_dir/release_pre"; do sleep 0.05; done
EOF
cat > "$probe_root/start" <<'EOF'
#!/bin/sh
probe_dir=$(dirname "$0")
touch "$probe_dir/start_entered"
while test ! -e "$probe_dir/release_start"; do sleep 0.05; done
EOF
chmod 700 "$probe_root/condition" "$probe_root/pre" "$probe_root/start"
cat > "$probe_unit" <<EOF
[Unit]
Description=Inert GPU gate activation probe
[Service]
Type=exec
TimeoutStartSec=60s
RuntimeMaxSec=120s
ExecCondition=$probe_root/condition
ExecStartPre=$probe_root/pre
ExecStart=$probe_root/start
EOF
systemctl --user daemon-reload
systemctl --user --no-block start "$probe_name"
```

Wait for `pre_entered`, then observe the held interval *after*
`condition_passed` and *before* `start_entered`:

```sh
for probe_try in $(seq 1 100); do
  test -e "$probe_root/pre_entered" && break
  sleep 0.05
done
test -e "$probe_root/condition_passed"
test -e "$probe_root/pre_entered"
test ! -e "$probe_root/start_entered"
systemctl --user show "$probe_name" --no-pager \
  -p ActiveState -p SubState -p Job -p MainPID -p ControlGroup
(
  for probe_try in $(seq 1 100); do
    systemctl --user show "$probe_name" --no-pager -p ActiveState -p Job
    test -e "$probe_root/start_entered" && break
    sleep 0.02
  done
) > "$probe_root/state_trace" &
probe_sampler=$!
touch "$probe_root/release_pre"
wait "$probe_sampler"
for probe_try in $(seq 1 100); do
  test -e "$probe_root/start_entered" && break
  sleep 0.05
done
systemctl --user show "$probe_name" --no-pager \
  -p ActiveState -p SubState -p Job -p MainPID -p ControlGroup
cat "$probe_root/state_trace"
```

Expected evidence in the held interval: both earlier markers exist, the
start marker does not, and `ActiveState=activating` or a recognizable
nonzero `Job` is visible. After release, the start marker appears and the
unit reports its new process/cgroup. An `inactive` state with `Job=0` during
the held interval fails the commissioning gate. Even a passing sample does
not prove every scheduling interleaving; repeat the observation around
release and keep the path disabled if the transition cannot be established.

Always clean up the exact temporary unit and scripts, including after a
failed observation:

```sh
touch "$probe_root/release_pre" "$probe_root/release_start"
systemctl --user stop "$probe_name"
rm -f -- "$probe_unit"
systemctl --user daemon-reload
rm -r -- "$probe_root"
```

The first reviewed test would install the adapter and one **disabled** test
unit, add the miner `ExecCondition`, observe gate behavior with fake lease
state, then invoke only the test unit's fixed synthetic worker. Inspect lease,
unit/cgroup, and KFD state before and after one request. The ordinary Ollama
port and Workbench tunnel stay unchanged. Rollback stops the test unit,
requires positive proof that its cgroup and GPU children ended, clears the
lease through the supervised path, verifies miner policy resumes, then removes
the test unit and drop-in. If proof is missing, leave the miner gated and
perform reviewed operator recovery.

Live approval is needed for installing/enabling the adapter, adding the
`ExecCondition` and disabled test unit, the controlled miner stop/restart, and
one bounded synthetic Ollama inference. No live step is authorized by this
source candidate.

## Cost and limits

An isolated server pays process startup, model discovery, cold model load,
and teardown on every request. It does not reuse a warm model or GPU KV cache
in the ordinary Ollama service. Both instances can read the same on-disk
model cache, but may compete for filesystem cache, memory, and VRAM if the
ordinary service has a loaded model or takes a concurrent direct request.
The first live test therefore needs a quiescent ordinary Ollama service and
fresh `/api/ps` plus GPU observations; a new direct call remains outside this
reservation path. A private route or tunnel cutover for periodic Workbench
reports is later work, after this one-request path is validated. No throughput
or latency improvement is claimed.
