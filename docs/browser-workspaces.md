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
tabs through an existing loopback DevTools endpoint and opens only missing
configured entries. Supply the expected user-data directory of that same Chrome
instance before a refresh or organization attempt:

```sh
bin/ai-workbench chrome --profile /path/to/existing/isolated-profile control-status
bin/ai-workbench chrome --profile /path/to/existing/isolated-profile refresh
```

The profile must already exist and the DevTools listener must be owned by its
Chrome process. If the profile is missing, differs from the listener, or cannot
be verified, the command fails before opening or closing tabs. New tabs and
windows are launched with that profile explicitly, then checked through the
same browser connection. A disconnected or restarted browser fails verification.
Refresh reports requested URLs separately from verified, partial, and uncertain
outcomes. It stops after a destination cannot be verified, avoiding additional
requests in an uncertain state. Recheck the browser before retrying a partial
result, because a delayed Chrome launch may still finish.

It never closes unrelated tabs. The CLI only controls the local browser.

The authenticated Workbench API and MCP tools can edit desired state only when
`WB_BROWSER_WORKSPACES_FILE` is explicitly set for the API service. Without it,
these routes return `503 browser_workspace_not_configured`; they do not claim a
desktop change. The configured absolute path must identify the **same file**
read by the desktop launcher, with both processes running under the same Unix
user on a shared filesystem. Keep the file and its parent private; the API
requires a caller-owned `0600` file in a caller-owned private directory. The
standard separate-user `workbench` service cannot edit a desktop user's
private config, so use the desktop CLI unless such a same-user shared setup is
deliberately configured. No browser tabs change until the local launcher acts.

### Current Chrome profiles

Chrome 136 and later ignore a remote-debugging port for the normal user-data
directory. These commands require an already connected, isolated profile and
do not start a debugging listener or modify Chrome security settings. An
isolated profile does not contain normal-profile tabs, accounts, or state.

## Organizing live tabs

`chrome organize` uses Chrome's local DevTools endpoint to preview the tabs that
match entries in one selected workspace. It changes nothing until `--apply` is
given:

```sh
bin/ai-workbench chrome --profile /path/to/existing/isolated-profile --workspace default organize
bin/ai-workbench chrome --profile /path/to/existing/isolated-profile --workspace default organize --apply --expect TOKEN
bin/ai-workbench chrome --profile /path/to/existing/isolated-profile --workspace default organize --apply --expect TOKEN --action move
```

The preview reports each selected tab, the matching entry, the matching rule,
and a target-set token. Copy its `expect` value into `--expect` when applying.
If a selected tab navigates, closes, or changes identity, or if the workspace
configuration changes, apply rejects the token; preview again.
Applying opens those selected URLs in a separate Chrome window for that named
Workbench workspace. The default `copy` action leaves every existing tab in
place. `move` is explicit: it closes each selected source only after a new target
with the same URL is observed in the connected browser. A missing or changed
destination leaves its source open. Results distinguish requested, verified,
partial, and uncertain copies, along with confirmed source closes. If the
connection fails after a close request, source state is reported as uncertain.
Unrelated tabs are never selected or closed.

Chrome's tab-group API is extension-only, so the local launcher uses a separate
window as the supported workspace boundary. It does not install an extension,
send tab URLs to the Workbench service, or run organization at browser startup.
