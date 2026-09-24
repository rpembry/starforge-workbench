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
