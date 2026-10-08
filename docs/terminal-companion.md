# Codex terminal companion: bounded infrastructure

This milestone adds a focus-only core for issue #278. It does not enable a live
Codex companion yet. The CLI reports `current_tui_identity_unavailable` and
refuses `toggle` until an owner can verify the exact conversation displayed in
an existing managed terminal pane. No provider, conversation, or daemon starts.

For an enabled Codex context in the existing manifest:

```text
aiw companion status CONTEXT
aiw companion shortcut CONTEXT
```

`shortcut` returns a descriptor with preferred Cmd+I and candidate Linux Super+I.
It installs nothing. Local installation requires verification of the terminal's
actual key event, existing binding conflicts, and a supported identity adapter.
The existing Fn+E clipboard editor remains separate.

## Focus and identity contract

The trusted owner composes the core with metadata probes, the existing tmux
server, and an identity scope that serializes conversation changes with focus.
The witness must bind the exact thread, display generation, pane, process
incarnation, and server incarnation. A caller-provided ID, launch catalog entry,
resume argument, static pane property, or loaded-thread list cannot establish
which conversation is currently displayed. The CLI accepts no identity override.

[Codex App Server documentation](https://learn.chatgpt.com/docs/app-server)
describes thread listing and reading; it does not establish the current TUI
thread-to-pane mapping needed here. Read-only inspection of Codex CLI 0.161.0
help and generated protocol schemas did not establish that mapping either.
The positive adapter in tests is deliberately synthetic and fenced. It is not a
live identity implementation or a production readiness claim.

The core switches one existing attached client between two existing sessions on
one server. The source must be an active shell pane; the destination must match
the owner's registered pane exactly. It rechecks client, pane, process, server,
and conversation before and after focus. A final recheck follows the durable intent write. Tmux metadata checking and
client switching are not atomic: a human focus change can still race the final
check/switch. A live owner would need client/lifecycle serialization as well as
the conversation fence before making a stronger guarantee.
The adapter offers only the fixed
`switch-client` operation. It captures no terminal content, sends no keystrokes,
runs no shell commands, and changes no layout, mouse setting, or binding.

A private local journal holds bounded focus metadata only. Intent is durable
before focus. Restart preserves completed return state; an interrupted or
unacknowledged operation becomes uncertain and refuses automatic replay.
Unknown, stale, foreign, replaced, or switched identities refuse focus.
This journal is not an authentication boundary against its owning OS account.

## Context sharing and validation

Opening a companion sends nothing. Context sharing is manual, reviewed copy and
paste by the human. No capture, preview persistence, automated paste, or Enter
operation is included in this milestone. A later sharing flow must separately
revalidate both endpoints and preserve explicit human control.

Synthetic tests cover repeated return, restart, crash/uncertain state, source and
target replacement, conversation switching, client replacement, and denied
caller identity overrides. The opt-in tmux smoke uses a unique guarded socket,
two inert shell stand-ins, and its own attached client. It never contacts a real
provider or an existing tmux server:

```text
SF_COMPANION_TMUX_TEST=1 PYTHONPATH=src pytest -q tests/test_terminal_companion_tmux.py
```

A supported live identity adapter and separately authorized real-provider smoke
remain prerequisites for an actual user trial. No local shortcut installation
or live configuration changes are part of this draft.
