# Adapting the reference

The planned private work-item workflow has a [separate guide](work-item-workflow.md).
Its local registry and task commands are staged work, not part of this release.

Start by inspecting `src/starforge_workbench/cli.py` for launcher/provider assumptions and `src/workbench/` for API, state, and collection behavior. The public snapshot combines the service, attention view, launcher, and provider collectors from separate development slices. Their combined automated tests are useful evidence; they are not proof of compatibility with every provider release or desktop.

## Launcher

Copy `config/workbench.example.yaml` to the private XDG configuration location, such as `~/.config/starforge-ai-workbench/workbench.yaml`, and adjust each directory, provider, and enabled flag. A checkout-local `config/workbench.yaml` is also supported for development and adaptation. The example intentionally contains no production or employer bindings. The launcher resolves an explicit `--manifest` first, then the private XDG manifest, then a checkout-local manifest, and finally the example. `bin/ai-workbench --dry-run up` previews the plan; `bin/ai-workbench doctor CONTEXT` checks prerequisites. Launch a configured context with `bin/ai-workbench up CONTEXT`, or add `--headless` to omit a graphical tab.

Provider executables are discovered on PATH, with historical installation-path fallbacks in `PROVIDERS`. The launcher still expects Linux process metadata, tmux, and zsh. The desktop bridge uses `/usr/bin/python3` with GObject introspection and the installed Ptyxis schemas. An optional `~/bin/ren.sh` title helper is attempted; its absence does not prevent provider startup. CLI flags, resume catalogs, and provider installations are expected adaptation points.

For an existing managed tmux session, run `aiw c Example Support` from an interactive terminal on the same host as Workbench, or SSH to that host first. The full configured title, context ID, and established launcher aliases work; names must match completely, though case is ignored. Use `aiw list` to discover titles and IDs. `c` attaches another client to the session even if a desktop client is present; inside the same Workbench tmux server it switches the current client. A missing or dead session is reported and never started by `c`. This command does not connect across hosts, create a session, or submit text to a provider pane.

Launcher metadata probes are bounded and distinguish confirmed absence from unknown
state. See [tmux probe behavior](tmux-probes.md) for recovery semantics and isolated
test coverage. Interactive attachment remains unbounded.

Runtime state, locks, and exact conversation bindings live under `~/.local/state/starforge-ai-workbench`. The dedicated tmux server is also named `starforge-ai-workbench`. Do not run this copy alongside another installation using that same runtime namespace without first isolating it.

## Codex Agents command center

For an optional persistent command-center tab, add a local context with
`provider: codex`, `codex_mode: agents`, `resume_policy: never`, and no
additional directories. Give it its own context ID and title. Then use
`ai-workbench up CONTEXT` as usual; repeated launches reuse the live tab.

This mode runs `codex app-server daemon start` before `codex agents -C CWD`.
Daemon startup is bounded and a failure prevents the command center from
opening. It never restarts or updates a running daemon. The installed Codex
must support these commands. The command center has no conversation binding
and does not acquire a checkout lock. Existing conversation contexts retain
their behavior. Workbench does not start tabs at login; if daemon availability
at login is desired, configure a private user service to run the same
idempotent daemon-start command.

For a Codex conversation that starts in a directory outside its role-specific
instructions, set `codex_instructions_file` in that context to an absolute
path to a private UTF-8 file. Workbench reads it on each new local launch and
resume and passes its content as Codex `developer_instructions`. A new
daemon-backed thread receives the content through app-server when created.
Missing, symlinked,
nonregular, nonprivate, or oversized files prevent the provider from starting.
Changing the file does not change an existing conversation binding; the next
local launch or resume receives the updated text; an existing daemon-backed
thread keeps the instructions it received when created. A currently running
conversation does not reload it. Keep secrets out of this file: local-mode
text is passed as a process argument and may be visible to other processes. Codex still
discovers ordinary `AGENTS.md` files from the working directory as usual.

For explicit specialist handoffs, keep a private mode-0600 YAML roster outside
the repository, for example at
`~/.config/starforge-ai-workbench/agent-roster.yaml`:

```yaml
version: 1
agents:
  - context_id: example-support
    description: Handles local system diagnostics and maintenance.
```

Only listed, enabled, daemon-backed Codex contexts can receive a handoff.
`ai-workbench handoff list` shows the roster; `ai-workbench handoff preview
example-support` returns the exact current session ID and idle/busy status.
After choosing one target, write the request to a private mode-0600 file and
call `ai-workbench handoff send example-support --session-id ID
--message-file FILE --key STABLE-UNIQUE-KEY`. The key prevents a retry from
submitting the same request twice. A timeout or crash after submission begins
is recorded as uncertain; inspect the target conversation before making a
new request. The command returns acceptance by the daemon, not completion of
the work. Remove the request file when it is no longer needed.

For a conversation that must share the local Codex app-server daemon with the
Agents command center, opt in with `codex_remote_daemon: true`,
`resume_policy: explicit-session`, and no additional directories. Workbench
starts the daemon if needed, creates a named thread through app-server with
the private `codex_instructions_file` content, saves its exact binding, then
opens it through `--remote unix://`. On restart it resumes that exact thread.
Existing local bindings are not silently converted: a deliberate migration
must preserve the old thread and select a new daemon-backed binding. If thread
creation has an uncertain outcome, Workbench stops rather than creating a
possible duplicate. Other contexts retain their current transport. The Codex
CLI must support local Unix-socket remote mode. A remote conversation's turns
are visible to other authorized clients of that daemon, so use this only when
shared control is intended.

To migrate one existing local context, save any draft and exit its Codex TUI,
then enable daemon mode in the private manifest. Run
`ai-workbench migrate-codex CONTEXT --session-id OLD-ID`. The command checks
that the exact old provider process has exited, saves a private backup of its
binding, creates a new named daemon thread with the context's current role
instructions, and binds it. The old Codex conversation remains in Codex's
history. Reopen the context with `ai-workbench up CONTEXT`. A failed or
uncertain migration does not retry thread creation automatically; inspect the
private creation intent and daemon thread list before resolving it.

## Mouse scrolling and terminal preferences


Ask AIW to configure scrolling when a terminal app behaves differently:

> Make the mouse wheel scroll output in my tabs. Check which apps manage their
> own scrolling, preserve live sessions, and save the settings for future launches.

The dedicated server loads [`config/tmux.conf`](../config/tmux.conf). The wheel
is forwarded to an application only when its pane uses the alternate screen,
the application requests mouse input, and tmux copy mode is inactive. This lets
full-screen Claude and OpenCode interfaces scroll their own output, including
when their context IDs change. Other panes use tmux scrollback. Copy mode keeps
its own scrolling, with vi keys (`q` returns to the app).

The decision is evaluated for each wheel event, so entering or leaving a
full-screen application changes routing immediately. Keep mouse reporting on.
The saved bindings load when the dedicated server starts, including after a
reboot; an existing server needs the installed configuration reloaded after an
upgrade. A terminal that intercepts mouse events may need separate configuration.

AIW should check the live bindings and the configuration used by the installed
launcher, apply changes only to the dedicated Workbench server, and confirm the
result with you. Reloading that configuration does not require restarting
providers. A live binding alone lasts only until the server exits; retain the
change in source and in the installed configuration so a reboot or later release
does not silently undo it. Personal overrides belong outside public Git; shared
behavior fixes should be committed and reviewed. Mouse selection, clipboard, and
link-opening preferences can also be requested, but depend on the terminal app
and should be checked separately.

## Daily operator conversation role

An optional private manifest context may select `role: daily-interface` for an
exact-resume Codex conversation. Workbench then supplies its public, provider
neutral role policy as additional developer instructions on a new launch and on
reconnecting the **same saved conversation**. Personal wording and destination
names remain in private local instructions. The role keeps Workbench status and
authorized operations in the daily conversation, routes development and
substantial research to the appropriate separate context, and treats explicit
handoff as a distinct request. It does not change a running conversation or
its tmux binding. To validate without touching an existing session, use a
synthetic manifest and inspect `ai-workbench --manifest PATH --dry-run up` plus
the isolated tests; a real reconnect requires a separately planned idle window.

The MCP preview/send path is described in [mcp-server.md](mcp-server.md).
Delivery supports a verified OpenCode registration through its instruction
queue or a private-roster Codex thread through the local shared daemon.
Unsupported providers need a copyable handoff.
Neither role selection nor a handoff authorizes the destination agent to start
work, merge, deploy, or change bindings.

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

Keep the two-token `WB_CREDENTIALS_FILE` on the API server. A third, distinct
`viewer` token is optional. It authorizes only `GET /api/attention`; all other
protected routes still require their existing role. For a separate collector
process, provision only its collector token in a private, owned,
mode-0600 client file such as:

```json
{"url":"http://127.0.0.1:8027","role":"collector","token":"<the collector token>"}
```

Pass that path as the collector's `--credentials-file` (or `WB_CLIENT_CONFIG`).
The client refuses to use this file for operator calls or another URL. Existing
two-token server files still work with local clients for compatibility, but do
not copy the server file to a collector host.

For `wb-notify`, provision a separate role-bound client file with the configured
API URL, `"role":"viewer"`, and the actual viewer token. The notifier selects
viewer authority when configured. Existing two-token server files and
operator-bound notifier files remain usable, but those legacy setups still
carry operator authority. No viewer token or access grant is created by this
code change.

Local mode authenticates with bearer headers. An ordinary browser navigation does not automatically supply those headers. The deployed browser flow is designed around Cloudflare Access; adapt its configuration before expecting interactive browser login.

In Cloudflare mode, set `WB_AUTH_MODE=cloudflare`, `WB_ACCESS_CONFIG` to a file containing `issuer`, `audience`, `browser_emails`, and `service_roles`, and `WB_PUBLIC_ORIGIN` to your HTTPS application origin. `service_roles` maps verified service identities to `operator`, `collector`, or optional `viewer`. Inspect `CloudflareAuth` and its tests for the exact schema. Cloudflare machine client configuration includes its HTTPS `url`, `auth_type: cloudflare`, and role-specific `client_id`/`client_secret` values. Keep those files outside Git.

## Collectors and deployment

Read each observer's CLI help and tests before enabling it. The process collector can be tried with `wb-collect --manifest PATH --context CONTEXT --dry-run`; it requires the host's process namespace and existing Workbench tmux sessions. Shipped collector units read `%h/.config/starforge-ai-workbench/workbench.yaml` and select contexts explicitly; adapt those selections to your manifest. A systemd drop-in can override `ExecStart` for a different manifest, including one under a custom `XDG_CONFIG_HOME`. The installer checks the manifest named by the effective user unit and requires an owned, nonsymlink regular file with no group or world permissions before enabling it. Omitting `--context` collects every enabled context.

Registered-session publishing by the launcher collector is opt-in. Pass
`--registration-state` (or `WB_REGISTERED_SESSION_STATE`) with an absolute path
outside Git in an owned `0700` directory; the collector creates a `0600` file
containing random opaque registration IDs, process/binding fingerprints, and
observation sequences. `--launcher-state` defaults to the launcher's protected
runtime root and is read only. Only enabled, selected contexts with a verified
live provider process are published. A live context without an exact protected
launcher binding is published as `unknown` with `exact_binding_missing`, which
is explicitly not a controllable target. The collector reads no terminal
contents, prompts, responses, or provider transcripts and performs no provider
or tmux mutation. When the exact process or binding generation changes, the old
opaque registration is marked `stopped` before a new identity is published.
Existing service recipes do not enable this option.

Claude collection starts with a persisted cutoff covering the preceding day. OpenCode collection starts at installation. The Codex observer's `--bootstrap-file` refers to a protected JSON file with a `snapshot` path to the legacy Worklog database; its bootstrap reads the old cursor and source identities. That historical schema is specific to the migration this grew from. Adapt or replace this bootstrap when starting without Worklog. The public importer only treats `user:` and GitHub source records as explicit; add your own trusted source rules deliberately. Never fabricate production records just to satisfy an installer.

`deploy/install-collector.py` expects a versioned release layout and protected client configuration. Options enable additional observer services. `deploy/install-release.sh` installs the server under `/opt/workbench` with state under `/var/lib/workbench`. Inspect paths, dependencies, service actions, and access configuration before running either helper. These recipes are not a universal installation workflow.

The release installer preserves an existing `/etc/workbench/service.env` byte-for-byte and requires it to be a regular, nonsymlink file owned by `root:workbench` with mode `0600` or `0640`. A first install with no such file must explicitly pass `--init-service-env` to initialize it from the example; otherwise installation stops before switching the active release or activating services. Review the initialized private values before using the service.

### Upgrade and rollback

Before switching a server release, the installer reads the exact `WB_DATABASE` path from `/etc/workbench/service.env` as data. It requires one unquoted absolute path assignment and stops before changing the service if the setting is missing, ambiguous, or points to a missing database on an upgrade. The supported unit layout is the shipped `workbench.service` without per-service or shared `service.d` drop-ins or alternate unit files. The installer checks systemd's reported unit search paths and rejects those override layers before stopping the service, since they could change the effective `WB_DATABASE`. Adapt and verify the backup procedure separately for a modified unit. The installer then stops `workbench.service` and saves an integrity-checked SQLite backup of that configured database under `/var/lib/workbench/backups` as the `workbench` user, printing both source and backup paths. The previous release target is recorded in the root-private `/opt/workbench/rollback-target` file before the `current` symlink changes. A first installation records `none`. The installer then starts the service and waits for the loopback `/healthz` endpoint; an active systemd unit alone does not count as a successful install.

If the health check fails, inspect the service logs and the recorded target. Manual rollback is: stop `workbench.service`, relink `/opt/workbench/current` to the recorded release, and restart. Restore the printed backup before restarting **only if** the new release migrated the database schema; an older release cannot open a newer schema. Preserve the failed database for diagnosis, and verify the restored backup with `PRAGMA integrity_check` before using it. Backup retention is left to the operator. For an explicit snapshot, run `deploy/backup-state.py --database PATH --dest PRIVATE_DIRECTORY` as the database owner.

No task execution scheduler is implied by run heartbeats or attention records. Reporting time windows and the default human actor label are simple personal conventions to adapt.

## Personal display and reporting settings

By default, new human actions use `Operator` and reports use `America/New_York`.
To change either without editing tracked source, copy the synthetic
[`config/settings.example.yaml`](../config/settings.example.yaml) outside the
repository, then set `WB_SETTINGS_FILE` to that file's absolute path before
starting the service:

```sh
export WB_SETTINGS_FILE="$HOME/.config/starforge-ai-workbench/settings.yaml"
```

The only precedence rule is: an explicitly set `WB_SETTINGS_FILE` overrides
both defaults; without it, both defaults apply. Settings load once at app
creation, so restart or reload the service after changing the file. Two
installations can use the same checkout and separate private settings files,
such as one naming its human actor `Avery Example` in `America/Los_Angeles`
and another retaining the defaults.

`human_name` must be a non-empty string of at most 500 characters and
`reporting_timezone` must be an installed IANA timezone name. Invalid or
unreadable settings stop startup with a field-specific error that does not
print configuration values. An omitted actor for a newly created human API
action uses `human_name`; an explicitly supplied actor, provider identity, and
stored record are not changed. Reporting timezone affects report calculations
and metadata only. Historical date-only imports retain their legacy timezone
provenance and are not rewritten or reinterpreted.

## OpenCode attention observations

The optional [`config/opencode-attention.example.js`](../config/opencode-attention.example.js)
bridge targets OpenCode `1.18.30`. Its contract was verified against the
published `@opencode-ai/plugin` and `@opencode-ai/sdk` `1.18.30` TypeScript
declarations and pinned runtime source ([plugin package](https://www.npmjs.com/package/@opencode-ai/plugin/v/1.18.30),
[SDK package](https://www.npmjs.com/package/@opencode-ai/sdk/v/1.18.30),
[prompt loop](https://github.com/anomalyco/opencode/blob/v1.18.30/packages/opencode/src/session/prompt.ts),
[run state](https://github.com/anomalyco/opencode/blob/v1.18.30/packages/opencode/src/session/run-state.ts),
[runtime permission schema](https://github.com/anomalyco/opencode/blob/v1.18.30/packages/schema/src/v1/permission.ts)). It uses
these structured hooks only:

- `chat.message` activates a turn generation using the user message ID and its
  OpenCode creation time.
- `message.updated` links an assistant message to its parent user message. A
  typed error becomes `provider_error`. Completion timestamps are not treated
  as idle because OpenCode also completes intermediate tool-call messages.
- `message.part.updated` links a tool `callID` to its assistant message and
  originating generation before a question callback can be classified. A
  terminal `completed` or `error` state explicitly resolves that question only
  when the bridge emitted its open observation first. An orphan terminal part
  is ignored before sequence allocation and suppresses a delayed before hook
  for the same call.
- `permission.asked` becomes `permission_wait` only after its nested
  `tool.messageID` has been linked to the current generation. `permission.replied`
  resolves the same permission identity through `requestID`. Requests without
  a linked tool identity remain unknown. The API also accepts the old
  `opencode.permission.updated` provenance for already queued evidence.
- `tool.execute.before` becomes `user_question` only for OpenCode's built-in
  `question` tool and a call ID previously linked to the current generation.
  Its arguments are discarded.
- Generation-less `session.status` and `session.idle` events are ignored. They
  cannot safely attribute delayed idle, cancel, queued-turn, or resume ordering
  to a user-message generation.

The bridge writes a private local JSONL queue. It is disabled when
`WB_OPENCODE_ATTENTION_EVENTS` is unset. To adapt it, create an owned directory
with mode `0700`, copy the example into an OpenCode plugin directory, point the
environment variable at an absolute queue path in that directory, and pass the
same path to `workbench.opencode_observer --attention-events`. OpenCode loads
plugins at startup, so restart OpenCode after installing or changing the local
copy. Do not point two bridge instances at one queue.

The bridge emits only provider/session IDs, user-message generation ID, a
SHA-256 incident identity derived locally from a permission, question call, or
assistant-message ID, bridge instance ID, fixed state/reason/provenance values,
sequence, and timestamps. It
never writes prompts, responses, reasoning, questions, error messages, tool
arguments, permission patterns, model output, or credentials. The existing
observer submits these records with its collector credential; operators and
unauthenticated callers cannot submit them.

`chat.message` is the only authority that can establish a current generation.
An attention event cannot promote its own generation. Assistant parent IDs and
permission message links prevent late records from an older turn from being
attributed to a newer turn. The plugin assigns a contiguous sequence because
the plugin event contract does not expose event sequence
numbers. The API requires the next sequence and a strictly newer observation
clock, rejects duplicate/out-of-order evidence, and rejects a generation whose
OpenCode creation time is not newer than the current generation. Provider
creation time and plugin wall-clock observation time must be timezone-aware;
the API rejects clocks more than five minutes in the future.

Queues written by the earlier bridge may already contain an orphan resolution.
After validating its generation, source, provenance, clock, and exact next
sequence, the API records that sequence as an ignored no-op without creating or
closing an incident. This permits the next queued record to proceed. A
resolution for an incident that exists but is already closed remains rejected.

After observer restart, its byte cursor resumes the queue using byte offset,
filesystem device, and inode. Replacement always resets to byte zero, including
an equal-sized or larger replacement; same-file truncation resets when its size
falls below the offset. A response lost
after an accepted write is safe: replay is rejected as a duplicate and then
acknowledged locally. Queue rotation replays from the beginning against the
same server checks. Plugin restart intentionally forgets in-memory turn state;
until a new `chat.message` establishes a generation, lifecycle events are
ignored and status remains unknown. Verified permission and question replies
resolve their matching incidents; a newer generation supersedes incidents from
the prior turn. An unresolved incident remains visible after 90 seconds with
explicitly stale wording so a notification rate limit cannot erase it. It does
not claim the request is still open. A stopped bridge, missing queue, or missed
resolution therefore leaves current status unknown; process and collector
observations remain the fallback.

OpenCode `1.18.30`'s session status and idle events do not carry a generation,
so strong idle is unsupported. The generation-less `session.error` event also
remains ignored. The bridge does not distinguish authentication, rate-limit, or
other provider error subtypes. Subagents and child sessions need separate validation.
A [disposable live smoke check](opencode-attention-live-smoke.md) verified
question and permission lifecycles with OpenCode 1.18.30 and OpenAI Sol.
Typed provider errors remain fixture-tested only. These observations never change
an action, approve a request, authorize execution, or establish task
completion.

## Verification

Run `uv run pytest -q` for the combined automated suite. Browser tests need `WB_PLAYWRIGHT_MODULE` pointing to an installed Playwright Core module and a supported browser. `tests/integration_terminal.py` is an explicit graphical integration check, not part of routine pytest. The tmux startup tests use isolated fixture processes and a dedicated test server. A real reboot and your own daily workflow need separate validation.
