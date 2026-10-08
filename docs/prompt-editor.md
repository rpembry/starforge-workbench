# Existing editor: pending dictation or empty draft

This optional standalone helper adapts the existing `edit-clipboard` launcher
for issue #300. The name and desktop shortcut stay unchanged. It opens the same
isolated VS Code profile, with extensions, sync, autosave and hot exit disabled.
It does not read or write the clipboard and does not submit text to an agent.
Use ordinary manual paste/copy inside the editor when desired.

The default action opens completed pending dictation if present, otherwise an
empty draft. A private existing editor lock refuses a second launch. Every
opening gets a new private temporary file; existing saved drafts are retained.
A running editor's unsaved buffer is never read or overwritten by this helper.
Pending dictation is stored privately outside Git and survives editor failure,
interruption, cancellation and reboot. No partial dictation writes are accepted.

An explicitly authorized source supplies one completed UTF-8 file, up to 64 KiB:

```text
edit-clipboard --queue-dictation /absolute/private/completed-draft.txt
```

The helper reads only that selected private owned regular file, publishes one
immutable pending record atomically without replacement, and prints its random
operation ID. If pending dictation already exists, it refuses replacement.
It treats text as bytes, without executing or rewriting it. Queueing does not
open an editor. Press the existing shortcut to open it when ready.

Closing VS Code successfully cannot prove whether the human accepted or cancelled
the draft. Therefore it records a bounded successful handoff receipt but keeps
pending text. After explicitly confirming a completed handoff, the human may use:

```text
edit-clipboard --accept-dictation OPERATION_ID
```

This requires the unchanged pending operation and a matching successful editor
receipt, under the same editor lock. Stale/repeated acknowledgements refuse. It
clears only pending state; saved editor drafts remain. No acknowledgement is
inferred from model output, transcript content or window closing.

This is a same-OS-user workflow, not authentication against that user. Source,
pending record and receipt reads reject symlinks, FIFOs, unsafe ownership or
permissions, invalid UTF-8 and oversized input. A changed pending revision cannot
receive a successful receipt or be acknowledged through a stale operation ID.

Native-first remains the preference: verify Codex's existing dictation controls
before introducing any prompt-mediated start/finish interface. This helper does
not capture audio, subscribe to voice events, change microphone permissions,
install a shortcut, or enable a provider. Desktop Dictate filling an editable
composer does not establish a Linux TUI export-to-editor bridge.

Installation is optional and separate from repository code. For an authorized
local experiment, save an exact private backup and digest of the existing helper,
verify no active editor, then replace only that file atomically. Leave the desktop
binding and user profile intact. Rollback restores that exact backup atomically;
queued private dictation and saved drafts are preserved. Synthetic tests inject
an editor adapter and never launch VS Code or access a real clipboard.
