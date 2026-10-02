# Manual favorite application restore

`uv run wb-apps` is a local, manual desktop-entry restorer. It never starts a
Workbench provider session, changes Chrome tabs, or creates login autostart.
The later coding-agent restore slice is separate work; a `workbench` favorite
here means only its configured desktop application.

Put an explicit selection in a private file (default
`~/.config/starforge-ai-workbench/favorite-apps.yaml`). Its directory must be
owned by you and mode `0700`; the file must be a regular nonsymlink file owned
by you with mode `0600`. No personal selection belongs in Git. This **synthetic
schema example** needs installed, matching desktop entries and executables
before it could be used:

```yaml
version: 1
favorites:
  - name: Example Editor
    kind: native
    desktop_id: example-editor.desktop
    wm_class: example-editor
    executable: /usr/bin/example-editor
  - name: Example Notes
    kind: pwa
    desktop_id: example-notes.desktop
    wm_class: example-notes
    executable: /usr/bin/example-browser
    profile: Profile 2
    app_id: example-app-id
  - name: Example Terminal
    kind: terminal
    desktop_id: example-terminal.desktop
    wm_class: example-terminal
    executable: /usr/bin/example-terminal
  - name: Example Workbench
    kind: workbench
    desktop_id: example-workbench.desktop
    wm_class: example-workbench
    executable: /usr/bin/example-workbench
```

`desktop_id` resolves to one exact, regular, nonsymlink `.desktop` file in the
XDG application directories. The executable and any declared
`StartupWMClass` must match the private identity. A PWA desktop entry must
also have the exact `--profile-directory` and `--app-id` arguments. These
paths and flags identify the installed application; the CLI never executes a
configured command or arbitrary path. It launches only the resolved desktop
ID through `gtk-launch`.

```sh
uv run wb-apps list
uv run wb-apps preview 'Example Editor' 'Example Notes'
uv run wb-apps apply --token TOKEN_FROM_PREVIEW 'Example Editor' 'Example Notes'
```

`list` is read-only status for the configured selection. `preview` is also
read-only and returns each target's intended `launch`, `preserve`, `skip`, or
`refuse` action with evidence and a token. Apply takes exactly the same names
and token, then rechecks the allowlist and desktop entries under a private
lock. An identity change requires a new preview. Apply reports per target:
`already_present`, `verified_ready`, `uncertain`, or `refused`. Launcher
acceptance by itself is never readiness. Separate `launch_requested` and
`launcher_accepted` fields distinguish a request to the desktop launcher from
verified readiness. Already-running targets are preserved;
this slice does not focus their windows.

An unresolved launch is recorded in private local state before invoking
`gtk-launch`. Repeated or overlapping applies will not launch it again merely
because its window has not appeared. The receipt includes the verified kernel
boot ID. A same-boot unresolved launch remains blocked, including after a
failed launcher result; a verified ready window clears its receipt. After a
reboot, a fresh preview and complete absent inventory permit an explicit new
apply, because the old process cannot survive that boot change. A changed
desktop-entry identity still refuses. Legacy receipts without a boot ID fail
closed for operator review. If an app remains uncertain, inspect it manually
before deciding on any separate recovery action; this CLI has no force-retry
or pending-state reset command. Missing, ambiguous, changed, or
unverifiable identities fail closed. A target that cannot be verified does not
block a separate exact target in the same selection.

The real observation adapter currently requires an X11 session, `wmctrl`,
`gtk-launch`, exact window class, and matching `/proc` process identity. It
refuses launches on Wayland or when inventory is incomplete. Chrome/PWA window
ownership can be difficult to prove on some builds; those targets remain
uncertain rather than receiving a duplicate launch. Only synthetic fixtures
have been exercised for this slice. This workstation's observed desktop
session is Wayland, so no live application launch or focus smoke was run.
The X11 adapter treats `wmctrl -x` output as an `instance.class` tuple. It
matches the configured `StartupWMClass` exactly against either component and
requires matching process ownership; it never uses a substring match. A
malformed tuple belonging to the selected process, or an unverifiable tuple
when no selected window is confirmed, remains uncertain. Dotted or whitespace
`StartupWMClass` values cannot be separated reliably in this output and are
unsupported by this adapter.

For independent QA, use disposable desktop entries and executables in a
separate X11 session. Record the exact build and desktop-entry identities,
preview first, then verify launch count, actual window ownership/readiness,
repeat/concurrent apply, delayed startup, and preservation of an already-open
window. Do not substitute normal user applications for disposable targets.
