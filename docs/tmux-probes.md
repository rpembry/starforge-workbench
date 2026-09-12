# Bounded launcher tmux probes

Noninteractive launcher tmux commands have a three-second subprocess deadline.
Timeouts, transport failures, protocol mismatches and malformed metadata raise a
`TmuxUnknown` error with a short fixed reason. Diagnostics never include captured
pane output or arbitrary tmux stderr. `check=False` does not make a failed
observation mean absence: metadata callers use the common `probe` contract.

The launcher distinguishes:

- **Present:** a successful, validated session list and pane response, including
  the exact binding, session ID, dead flag and process ID.
- **Absent:** a validated session list without the requested name, an explicit
  `no sessions` result, or the expected missing-socket error with local absence
  confirmed. A refused connection/stale socket is not automatically removed or
  treated as proof of absence.
- **Unknown:** failed or contradictory observations, including a session found by
  the first probe but unavailable to the following pane query. Retry inspection;
  do not infer that the provider exited.

`status` labels these outcomes and `doctor` reports them per context. One uncertain
context does not prevent inspecting or attempting the remaining contexts. Unknown
observations abort create, respawn, binding writes and pending-receipt cleanup.
Newly created sessions must have a verified target before option writes; a
session disappearing during setup stops further work without guessing a target.
Respawn retains the no-kill behavior and uses the observed session identity.
Binding discovery and explicit rebind both check tmux state before writing.

The direct external-session tty discovery now uses the same bounded probe path.
Dead panes can legitimately have an empty tty; malformed live tty metadata remains
unknown. The separate API process collector already has its own ten-second bound
and reports scan failure rather than taking launcher actions; its read-only
collection contract is unchanged.

Interactive `attach-session` intentionally has **no timeout**, because its lifetime
is the user's terminal session. It uses the same explicit server/socket arguments,
a verified target, and a fresh final observation before clearing a pending receipt.
The launcher adds no automatic cleanup or destructive recovery policy.

Mocked tests cover missing socket/session, stale socket, malformed responses,
protocol mismatch, timeout, disappearance between queries, safe recovery, binding
and cleanup guards, direct tty probes, and continued reporting. The opt-in real tmux
test uses an explicit socket in a freshly created temporary directory and guards
all parent tmux commands against any other socket. It starts only a synthetic
provider, verifies bounded startup retries and repeat-up identity, injects an
unknown probe, verifies the fixture process survives, and cleans up only that
fixture socket. No live sessions are fixtures.

```sh
uv run pytest -q
WB_TEST_TMUX=1 uv run pytest -q tests/test_startup_tmux.py
```
