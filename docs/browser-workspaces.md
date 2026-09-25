# Chrome workspaces

Starforge Workbench owns the desired Chrome workspace in the private file
`~/.config/starforge-ai-workbench/browser-workspaces.yaml` (or the path named by
`WB_BROWSER_WORKSPACES_FILE`). It is separate from the launcher manifest and is
created with mode `0600`.

The launcher is available as `bin/chrome`, `bin/aiw chrome`, or
`bin/ai-workbench chrome`:

```sh
bin/ai-workbench chrome add Gmail https://mail.google.com/
bin/ai-workbench chrome list
bin/ai-workbench chrome update Gmail --url https://mail.google.com/mail/u/0/
bin/ai-workbench chrome remove Gmail
bin/ai-workbench chrome
```

If Chrome is not running, the default workspace URLs are opened. If Chrome is
already running, the launcher focuses it and does not duplicate the workspace.
Raw Chrome remains available through `google-chrome` (or the installed Chromium
equivalent).

`chrome refresh` is deliberately opt-in and non-destructive. It inspects page
tabs through a local DevTools endpoint and opens only missing configured origins.
Use `control-status` before a refresh or organization attempt:

```sh
bin/ai-workbench chrome control-status
bin/ai-workbench chrome refresh
```

It never closes unrelated tabs. The CLI only controls the local browser; the
authenticated Workbench API and MCP server expose desired-state CRUD so an AIW
conversation can list, add, and remove named workspace entries without editing
YAML.

### Current Chrome profiles

Chrome 136 and later deliberately ignore a remote-debugging port for the normal
user-data directory. Workbench never restarts the normal browser with a separate
profile, because that browser would not contain the tabs, accounts, or state the
operator asked to manage. If `control-status` reports
`needs_explicit_normal_profile_connection`, use Chrome's explicit, browser-owned
connection flow instead:

1. In the running browser, open `chrome://inspect/#remote-debugging`, enable
   Remote Debugging, and approve Chrome's permission dialog.
2. Configure a trusted local MCP client with Chrome DevTools MCP's
   `--autoConnect` option. The client connects to the normal profile only after
   that browser approval; it can list, open, and close browser pages.

For example, a local MCP client configuration can use:

```json
{
  "mcpServers": {
    "chrome-devtools": {
      "command": "npx",
      "args": ["-y", "chrome-devtools-mcp@latest", "--autoConnect", "--no-usage-statistics"]
    }
  }
}
```

This is a separate, explicitly approved local browser-control integration; it
does not send tab URLs to the Workbench service. The port-based Workbench
commands remain appropriate for a deliberately launched, isolated debugging
profile only, for example:

```sh
google-chrome --remote-debugging-port=9222 --user-data-dir=/tmp/workbench-chrome-profile
```

That separate profile has no access to the normal-profile tab set.

## Organizing live tabs

`chrome organize` uses Chrome's local DevTools endpoint to preview the tabs that
match entries in one selected workspace. It changes nothing until `--apply` is
given:

```sh
bin/ai-workbench chrome --workspace default organize
bin/ai-workbench chrome --workspace default organize --apply
bin/ai-workbench chrome --workspace default organize --apply --action move
```

The preview reports each selected tab, the matching entry, and the matching rule.
Applying opens those selected URLs in a separate Chrome window for that named
Workbench workspace. The default `copy` action leaves every existing tab in
place. `move` is explicit: only after the new window has been requested does it
ask Chrome to close the exact selected original tabs, then reports whether each
close was verified. Unrelated tabs are never selected or closed.

Chrome's tab-group API is extension-only, so the local launcher uses a separate
window as the supported workspace boundary. It does not install an extension,
send tab URLs to the Workbench service, or run organization at browser startup.
