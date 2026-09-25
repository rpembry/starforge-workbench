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

`chrome refresh` is deliberately opt-in and non-destructive. When Chrome is
started with a local DevTools endpoint, it inspects page tabs and opens only
missing configured origins:

```sh
google-chrome --remote-debugging-port=9222
bin/ai-workbench chrome refresh
```

It never closes unrelated tabs. The CLI only controls the local browser; the
authenticated Workbench API and MCP server expose desired-state CRUD so an AIW
conversation can list, add, and remove named workspace entries without editing
YAML.

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
