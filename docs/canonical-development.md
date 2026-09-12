# Canonical development and local configuration

Use `https://github.com/rpembry/starforge-workbench` for application changes,
reviews and engineering issues. The earlier private repository is a read-only
historical archive, not a second development branch. Its history and deployment
records were not imported into this repository.

The public application already includes the newer settings, notification,
attention, action/run linking and tmux improvements. The remaining reusable
Google Keep collector and its synthetic tests were brought over as source files,
without private history or runtime configuration. Live Keep extraction remains
an outstanding acceptance check.

## Keep installation state separate

Keep credentials, private manifests, operator instructions, collector cursors,
databases, browser data and deployment receipts in private directories outside
Git. The existing `~/.config/starforge-ai-workbench` and
`~/.local/state/starforge-ai-workbench` namespaces remain compatible; choosing a
canonical repository does not rename state or reset sessions.

For a fresh checkout, run `uv sync`. Supply a private manifest explicitly:

```sh
bin/ai-workbench --manifest ~/.config/starforge-ai-workbench/workbench.yaml list
```

An ignored `config/workbench.yaml` symlink to that private manifest is another
option for an existing installation. Never replace a working manifest with the
example without adapting and reviewing it.

## Existing installations

Preserve dirty checkouts and deployed release directories during consolidation.
Do not change a bound context's cwd, provider session ID or fingerprint merely
to rename a repository: that can invalidate a healthy session binding. Use the
canonical checkout for new development. Migrate an existing bound development
session separately, preserving its exact identity and following the operating
guide. Services and launcher symlinks should move only during a reviewed
installation update, with backups and validation of the existing private
configuration. A Git merge or repository archive is not a deployment.

Personal operating guides remain local. Generic behavior belongs in this
repository's documentation; private hostnames, inventories, account mappings
and historical operational records do not.
