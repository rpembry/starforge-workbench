# Provider capability evidence

This matrix separates implementation and fixture evidence from real-provider
acceptance. `Live` means only the exact claim was checked against an installed
executable; `fixture` means no real provider ran; `unsupported` means Workbench
deliberately does not claim the capability; `unknown` means there is no evidence.
Version discovery below was repeated on 2026-09-12 and did not start a session.

| Capability | Codex 0.154.0 | Claude Code 2.1.269 | OpenCode 1.18.30 | Antigravity 1.2.2 | Ollama 0.34.0 |
| --- | --- | --- | --- | --- | --- |
| Executable discovery/version | [Live](../src/starforge_workbench/cli.py): `codex --version` | [Live](../src/starforge_workbench/cli.py): `claude --version` | [Live](../src/starforge_workbench/cli.py): explicit installed path plus `--version` | [Live](../src/starforge_workbench/cli.py): `agy --version` | [Live](../src/starforge_workbench/cli.py): `ollama --version` |
| New-session argv | [fixture](../tests/test_launcher.py) | [fixture](../tests/test_reboot.py) | [fixture](../tests/test_opencode_launcher.py) | [fixture](../tests/test_reboot.py) | [fixture](../tests/test_launcher.py) |
| Exact resume | [fixture](../tests/test_launcher.py) | [fixture](../tests/test_reboot.py) | [fixture](../tests/test_opencode_launcher.py) | [fixture](../tests/test_reboot.py) | unsupported (`resume_policy: never`) |
| Process detection | [fixture](../tests/test_launcher.py) | [fixture](../tests/test_launcher.py) | [fixture](../tests/test_launcher.py) | [fixture](../tests/test_launcher.py) | [fixture](../tests/test_launcher.py) |
| Activity parsing | [fixture](../tests/test_observer_reports.py) | [fixture](../tests/test_claude_observer.py) | [fixture](../tests/test_opencode_observer.py) | unknown | unknown |
| Explicit attention reason | [fixture](../tests/test_attention.py) | [fixture](../tests/test_attention.py) | [fixture](../tests/test_attention.py) | [fixture](../tests/test_attention.py) | [fixture](../tests/test_attention.py) |

The attention row verifies explicit Workbench run/approval reasons, not inference
from provider output. Malformed, truncated, restart and replay behavior is covered
by the linked Codex, Claude and OpenCode observer fixtures. Strong OpenCode idle
detection remains **unsupported**: a completed message is activity evidence, not
proof that the application is idle. Live Google Keep extraction remains
**unknown/unverified** and is not represented by synthetic records or version checks.

Separately, a [disposable live check on 2026-09-12](opencode-attention-live-smoke.md)
verified OpenCode 1.18.30 with OpenAI Sol: question and permission requests
reached the shared attention API and dashboard, replies cleared them, and
observer restart/full queue replay did not restore cleared incidents. This
covers structured provider attention, distinct from the explicit run/approval
row above. Typed provider errors remain fixture-only; generation-less idle
remains unsupported. The check did not deploy the bridge to existing sessions.

The isolated tmux fixture is also fixture-only. Its shared guard creates a unique
`0700` directory and `0600` ownership marker, then strictly parses tmux's global
argv and permits only one separate `-S` selector for that owned socket. Attached,
duplicate and alternate selectors, named/default/production targets, changed
ownership and symlink targets are rejected before the subprocess runs, including
when `TMUX` is unset. Provider-command options are not mistaken for global options.
Cleanup rechecks the same ownership invariant before sending `kill-server` only to
the fixture socket. See
[`test_tmux_guard.py`](../tests/test_tmux_guard.py) and
[`test_startup_tmux.py`](../tests/test_startup_tmux.py).

## Opt-in smoke check after upgrades

Do not run this procedure in routine tests or against an existing context.

1. Record the date and output of each installed provider's `--version`; update
   the matrix only for versions actually observed.
2. Create a temporary manifest and disposable project containing no credentials,
   with one test context on a unique tmux socket/server. Run `--dry-run up` first.
3. With explicit approval, start only that context and confirm new-session argv,
   provider PID, pane cwd and first-paint dimensions. Do not type a real task.
4. For providers that expose stable local session IDs, exit normally, bind the
   exact synthetic ID, resume it once, and confirm the same ID. Never guess by title.
5. Feed only synthetic malformed/truncated/replayed records to collectors and
   verify explicit attention reasons. Do not inspect or upload private transcripts.
6. Stop the synthetic provider normally, then clean up only the unique fixture
   socket after confirming its resolved path is not default, inherited or live.

Record failed, skipped and unavailable checks as `unknown` or `fixture`; do not
promote them to `Live`. Real-provider TUI rendering remains manual acceptance.
