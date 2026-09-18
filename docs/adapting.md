# Adapting the reference

Start by inspecting `src/starforge_workbench/cli.py` for launcher/provider assumptions and `src/workbench/` for API, state, and collection behavior. The public snapshot combines the service, attention view, launcher, and provider collectors from separate development slices. Their combined automated tests are useful evidence; they are not proof of compatibility with every provider release or desktop.

## Launcher

Copy `config/workbench.example.yaml` to `config/workbench.yaml` (ignored by Git). Adjust each directory, provider, and enabled flag. The example intentionally contains no production or employer bindings. `bin/ai-workbench --dry-run up` previews the plan; `bin/ai-workbench doctor CONTEXT` checks prerequisites. Launch a configured context with `bin/ai-workbench up CONTEXT`, or add `--headless` to omit a graphical tab.

Provider executables are discovered on PATH, with historical installation-path fallbacks in `PROVIDERS`. The launcher still expects Linux process metadata, tmux, and zsh. The desktop bridge uses `/usr/bin/python3` with GObject introspection and the installed Ptyxis schemas. An optional `~/bin/ren.sh` title helper is attempted; its absence does not prevent provider startup. CLI flags, resume catalogs, and provider installations are expected adaptation points.

Launcher metadata probes are bounded and distinguish confirmed absence from unknown
state. See [tmux probe behavior](tmux-probes.md) for recovery semantics and isolated
test coverage. Interactive attachment remains unbounded.

Runtime state, locks, and exact conversation bindings live under `~/.local/state/starforge-ai-workbench`. The dedicated tmux server is also named `starforge-ai-workbench`. Do not run this copy alongside another installation using that same runtime namespace without first isolating it.

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
