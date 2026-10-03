# Manual favorite application restore

The **Favorite Apps** application-menu entry opens a manual checklist. It
shows which configured favorites are already open, ready to open, or have an
unknown running status. Only favorites verified absent are checked by default;
already open, unresolved, and unknown-status apps can be selected manually.
Nothing is opened until the user previews and confirms. The user can go Back
to change the selection, and confirms
before opening them. Unknown status requires an explicit **Open anyway** choice
because another window may already exist. An unresolved same-boot launch is
labeled separately. Retrying it requires the user to check existing windows
and explicitly choose **I checked; retry** after a duplicate-window warning.
Each deliberate retry is recorded in a private audit file before it is sent to
the desktop launcher. The entry never starts apps at login. Back to the list
refreshes status without requesting a launch or discarding a receipt.
The result dialog says when all selected apps were already open, when no app
could be opened, or how many desktop launch requests were made and verified
ready. Launcher acceptance alone is never described as an opened app.

`uv run wb-apps` is a local, manual desktop-entry restorer. It invokes only the
selected installed desktop entry. A `workbench` favorite can therefore open a
configured Workbench terminal context. It does not create login autostart.

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
`gtk-launch`. Repeated or overlapping ordinary applies will not launch it again
merely because its window has not appeared. The receipt includes the verified
kernel boot ID. A same-boot unresolved launch is blocked unless the graphical
user explicitly confirms a duplicate-risk retry after checking existing windows.
The retry is audited locally before launch, and the original receipt remains
until a verified ready window clears it. After a
reboot, a fresh preview and complete absent inventory permit an explicit new
apply, because the old process cannot survive that boot change. A changed
desktop-entry identity still refuses. Legacy receipts without a boot ID fail
closed for operator review. The CLI has no force-retry or pending-state reset
command. Missing, ambiguous, changed, or
unverifiable identities fail closed. A target that cannot be verified does not
block a separate exact target in the same selection.

The graphical launcher also keeps up to 64 recent private attempt summaries in
`favorite-app-attempts.json` beside the favorites file. Each summary contains a
timestamp, boot ID, selected and action counts, outcome phase, and a limited
failure category. It stores no favorite names, desktop IDs, titles, URLs, or
content. Closing the checklist records a cancellation; a preview that cannot
open anything records a no-launch outcome. An audit write failure is reported
in the result dialog after an ordinary launch attempt. An unsafe retry-audit
file prevents the retry before a launcher request is sent.

The X11 observation adapter requires `wmctrl`, `gtk-launch`, exact window
class, and matching `/proc` process identity. On GNOME Wayland, the optional
read-only GNOME presence extension can provide native window presence. The
client checks its D-Bus owner against GNOME Shell, allowlist membership, and
sample freshness, then checks user processes before declaring absence. Browser
PWAs remain unknown unless an exact executable, same-profile, same-app browser process
proves presence. Unknown does not authorize an automatic launch. Only synthetic
tests have been exercised for this integration; live GNOME mapping and normal
app launches remain separate QA steps.
The X11 adapter treats `wmctrl -x` output as an `instance.class` tuple. It
matches the configured `StartupWMClass` exactly against either component and
requires matching process ownership; it never uses a substring match. A
malformed tuple belonging to the selected process, or an unverifiable tuple
when no selected window is confirmed, remains uncertain. Dotted or whitespace
`StartupWMClass` values cannot be separated reliably in this output and are
unsupported by this adapter.

The [GNOME 50 presence extension](gnome-favorite-presence.md) is optional and
read-only. Installing or enabling it is a separate review decision. If it is
disabled or unreachable, the dialog reports unknown status.

After installing the Python package into a stable user-owned environment,
install the menu entry with `bin/install-favorite-apps-desktop --executable`
pointed at that environment's absolute `wb-apps` path. The installer creates
only `starforge-favorite-apps.desktop` in the user's applications directory;
it refuses to overwrite a different existing entry. The private favorites
file must be configured separately. No app is opened by installation.

This is a local manual desktop feature. It adds no MCP tool, agent skill, or
background service; no existing MCP registration or skill needs to change.

For independent QA, use disposable desktop entries and executables in a
separate X11 session. Record the exact build and desktop-entry identities,
preview first, then verify launch count, actual window ownership/readiness,
repeat/concurrent apply, delayed startup, and preservation of an already-open
window. Do not substitute normal user applications for disposable targets.
