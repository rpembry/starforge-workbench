# GNOME 50 favorite presence: source-only review artifact

This issue #167 artifact is an **uninstalled, disabled-by-default** GNOME Shell
extension. It offers a narrow Wayland observation source for the manual favorite
restore design. It does not launch, focus, restore, or control applications, and
the existing X11-only `wb-apps` adapter does not consume it. Do not use a
`Snapshot` response to authorize a launch yet.

The source is in
[`extensions/favorite-presence@rpembry.github.io`](../extensions/favorite-presence@rpembry.github.io).
`metadata.json` pins GNOME Shell major version 50 and only the `user` session
mode. The extension exports one method, `Snapshot`, on the per-user session
bus at `org.starforge.Workbench.FavoritePresence` and
`/org/starforge/Workbench/FavoritePresence`. It takes no arguments and returns
the observation's monotonic microsecond timestamp and an array of
`(key, state, confidence, window_count)` for the private allowlist only. There
are no D-Bus properties, signals, control methods, remote listeners, or
arbitrary app-ID queries. It does not read or export titles, URLs, content,
focus history, PIDs, or process command lines.

The allowlist lives in the current user's GSettings/dconf state, outside Git.
Its schema has an empty default and accepts at most 32 triples of a local key,
exact desktop ID, and `native`, `browser`, or `pwa`. No actual favorite values
are in this repository or the package. Any same-user process with session-bus
access can read the returned keys and counts; this interface is intentionally
low-detail, not a secret store. Invalid, duplicate, missing, or unreadable
configuration never establishes absence.

For native entries, `Shell.WindowTracker` associates current Mutter windows
with Shell app IDs. An exact mapping can report `present` with confidence
`shell-app-association` and a count. Zero matching windows can report `absent`
with confidence `window-inventory` only when the exact desktop ID is installed,
the window inventory has no unmapped entries, and no startup sequence is
pending. This says nothing about a process that has not opened a window and is
**not** sufficient to launch without duplicate risk. GNOME's mapping is
heuristic. Browser and PWA entries always return `unknown`: Shell app IDs do
not prove a selected Chrome/Edge profile or PWA identity, particularly when
multiple profiles share a browser executable.

The interface is available only while the extension is enabled in the normal
unlocked user session. GNOME disables user-mode extensions on lock; the method
also checks session mode and lock state and returns `unknown` during a
transition. A missing bus name, disabled extension, call error, invalid
identity, or cached result older than two seconds must be interpreted by any
future consumer as `unknown`. The timestamp is monotonic and meaningful only
within the same boot. A client must verify that the D-Bus owner is the actual
GNOME Shell process before trusting even `present`; a same-user process can
otherwise claim the extension's well-known bus name when it is disabled. No
client or launch integration is included in this artifact.

## Compatibility and trust

GNOME's [extension anatomy](https://gjs.guide/extensions/overview/anatomy.html)
requires ES-module `enable()`/`disable()` cleanup. The extension owns and
unowns its session-bus name and exported object in those methods using the
[GJS D-Bus APIs](https://gjs.guide/guides/gio/dbus.html). It uses
[`Shell.WindowTracker`](https://gnome.pages.gitlab.gnome.org/gnome-shell/shell/class.WindowTracker.html)
for app mapping and the normal `user`
[session mode](https://gjs.guide/extensions/topics/session-modes.html).
GNOME's [50 porting guide](https://gjs.guide/extensions/upgrading/gnome-shell-50.html)
lists no relevant `extension.js` or metadata change. The public API pages
currently display version 51, so this remains source and mock tested against
the reported GNOME Shell/Mutter 50.5 workstation, **not live verified** there.
The browser versions observed during planning were Chrome 154.0.8037.57 and
Edge 153.0.4234.48; the extension makes no compatibility claim about their
profile or PWA identities.

A Shell extension executes as fully trusted GNOME Shell code, not a sandboxed
helper. An error can destabilize the desktop. Randall must approve the exact
reviewed source and package before installation or activation. No accessibility
setting, Shell introspection permission, third-party extension, or session
switch is part of this design.

## Review, activation, and rollback plan

The source can be checked without touching the live session:

```sh
uv run pytest -q tests/test_gnome_favorite_presence.py
glib-compile-schemas --strict --dry-run extensions/favorite-presence@rpembry.github.io/schemas
gnome-extensions pack --force \
  --out-dir=~/codex-output/starforge-workbench/issue-167-wayland-presence \
  --extra-source=presence-core.js \
  --schema=schemas/org.gnome.shell.extensions.starforge-favorite-presence.gschema.xml \
  extensions/favorite-presence@rpembry.github.io
```

After explicit approval of the exact package, an operator would record its
SHA-256, install it with `gnome-extensions install`, set the private GSettings
allowlist, enable only this UUID, and make a disposable read-only `Snapshot`
call. The live QA gate would verify D-Bus owner identity, fresh timestamps,
normal-session presence, lock/disable behavior, and native identity mapping
before any consumer integration. It would not launch normal applications.

Rollback is `gnome-extensions disable favorite-presence@rpembry.github.io`,
then verify the D-Bus name is gone. Uninstalling the exact UUID is a separate
cleanup step after disabling; private GSettings values can be retained for
review or reset deliberately. Disabling or uninstalling cannot undo a Shell
crash, which is why activation requires separate approval and live QA.
