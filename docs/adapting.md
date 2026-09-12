# Adapting the reference

Start by inspecting `src/starforge_workbench/cli.py` for launcher/provider assumptions and `src/workbench/` for API, state, and collection behavior. The public snapshot combines the service, attention view, launcher, and provider collectors from separate development slices. Their combined automated tests are useful evidence; they are not proof of compatibility with every provider release or desktop.

## Launcher

Copy `config/workbench.example.yaml` to `config/workbench.yaml` (ignored by Git). Adjust each directory, provider, and enabled flag. The example intentionally contains no production or employer bindings. `bin/ai-workbench --dry-run up` previews the plan; `bin/ai-workbench doctor CONTEXT` checks prerequisites. Launch a configured context with `bin/ai-workbench up CONTEXT`, or add `--headless` to omit a graphical tab.

Provider executables are discovered on PATH, with historical installation-path fallbacks in `PROVIDERS`. The launcher still expects Linux process metadata, tmux, and zsh. The desktop bridge uses `/usr/bin/python3` with GObject introspection and the installed Ptyxis schemas. An optional `~/bin/ren.sh` title helper is attempted; its absence does not prevent provider startup. CLI flags, resume catalogs, and provider installations are expected adaptation points.

Launcher metadata probes are bounded and distinguish confirmed absence from unknown
state. See [tmux probe behavior](tmux-probes.md) for recovery semantics and isolated
test coverage. Interactive attachment remains unbounded.

Runtime state, locks, and exact conversation bindings live under `~/.local/state/starforge-ai-workbench`. The dedicated tmux server is also named `starforge-ai-workbench`. Do not run this copy alongside another installation using that same runtime namespace without first isolating it.

## Isolated local API

After `uv sync --frozen`, create a private local credential file. The following generates fresh random values without printing them and refuses to overwrite an existing file:

```sh
uv run python - <<'PY'
import json, os, secrets
from pathlib import Path
root = Path.home() / '.config/starforge-ai-workbench-demo'
root.mkdir(mode=0o700, parents=True, exist_ok=True)
if root.is_symlink() or root.stat().st_mode & 0o077:
    raise SystemExit('Use a private, nonsymlink configuration directory')
fd = os.open(root / 'credentials.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as out:
    json.dump({role: secrets.token_urlsafe(48) for role in ('operator', 'collector')}, out)
PY

export WB_CREDENTIALS_FILE="$HOME/.config/starforge-ai-workbench-demo/credentials.json"
export WB_DATABASE="$HOME/.config/starforge-ai-workbench-demo/workbench.sqlite"
export WB_AUTH_MODE=local
uv run uvicorn workbench.main:create_app --factory --host 127.0.0.1 --port 8027 --no-access-log
```

From a second terminal, use the same credential-file environment variable:

```sh
uv run wb-api GET /openapi.json --url http://127.0.0.1:8027
uv run wb-api GET /api/attention --url http://127.0.0.1:8027
```

Local mode authenticates with bearer headers. An ordinary browser navigation does not automatically supply those headers. The deployed browser flow is designed around Cloudflare Access; adapt its configuration before expecting interactive browser login.

In Cloudflare mode, set `WB_AUTH_MODE=cloudflare`, `WB_ACCESS_CONFIG` to a file containing `issuer`, `audience`, `browser_emails`, and `service_roles`, and `WB_PUBLIC_ORIGIN` to your HTTPS application origin. `service_roles` maps verified service identities to `operator` or `collector`. Inspect `CloudflareAuth` and its tests for the exact schema. Cloudflare machine client configuration includes its HTTPS `url`, `auth_type: cloudflare`, and role-specific `client_id`/`client_secret` values. Keep those files outside Git.

## Collectors and deployment

Read each observer's CLI help and tests before enabling it. The process collector can be tried with `wb-collect --manifest PATH --context CONTEXT --dry-run`; it requires the host's process namespace and existing Workbench tmux sessions. Service units select contexts explicitly; adapt those selections to your manifest.

Claude collection starts with a persisted cutoff covering the preceding day. OpenCode collection starts at installation. The Codex observer's `--bootstrap-file` refers to a protected JSON file with a `snapshot` path to the legacy Worklog database; its bootstrap reads the old cursor and source identities. That historical schema is specific to the migration this grew from. Adapt or replace this bootstrap when starting without Worklog. The public importer only treats `user:` and GitHub source records as explicit; add your own trusted source rules deliberately. Never fabricate production records just to satisfy an installer.

`deploy/install-collector.py` expects a versioned release layout and protected client configuration. Options enable additional observer services. `deploy/install-release.sh` installs the server under `/opt/workbench` with state under `/var/lib/workbench`. Inspect paths, dependencies, service actions, and access configuration before running either helper. These recipes are not a universal installation workflow.

No task execution scheduler is implied by run heartbeats or attention records. Reporting time windows and the default human actor label are simple personal conventions to adapt.

## Verification

Run `uv run pytest -q` for the combined automated suite. Browser tests need `WB_PLAYWRIGHT_MODULE` pointing to an installed Playwright Core module and a supported browser. `tests/integration_terminal.py` is an explicit graphical integration check, not part of routine pytest. The tmux startup tests use isolated fixture processes and a dedicated test server. A real reboot and your own daily workflow need separate validation.
