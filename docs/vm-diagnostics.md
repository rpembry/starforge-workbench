# VM diagnostics confidentiality milestone

Related design: [#279](https://github.com/rpembry/starforge-workbench/issues/279).

`wb-vm-mcp` is a separate stdio facade for a synthetic-first Linux VM diagnostic
contract. The installed command has **no live transport and no registrations**.
It does not connect to a VM, load credentials, change host configuration, boot
or mount a disk, or provide an operational remote-control interface. Existing
`wb-mcp` behavior is unchanged.

```sh
uv run wb-vm-mcp --confidential
```

Confidential is the default even without a flag. `--standard` explicitly chooses
the broader startup profile, but this milestone still exposes only typed
diagnostics in both profiles; it implements no content exports or arbitrary
commands. Conflicting or unknown startup arguments fail with a fixed error.
Mode and operator-owned registrations are immutable after server construction.
No environment variable or MCP call can change them. The server has no exception
granting tool; broader-command exceptions from #279 remain future work.

## Contract and authority

Trusted local operator code may construct a `DiagnosticService` using frozen
`GuestRegistration` objects and a `DiagnosticTransport`. A registration contains
an opaque guest ID (`g_` plus 32 lowercase hex digits), a private adapter handle,
and a frozenset of permitted `Diagnostic` values. The MCP caller supplies only
the opaque ID and operation. It cannot supply a host, executable, path,
environment, shell argument, credential or registration. Unknown IDs and
unapproved operations deny before the transport runs, in either profile. This
registration is an execution-authority boundary for a deliberately trusted local
stdio client, not remote multi-user authentication.

`vm_diagnostic` accepts these fixed operations. `vm_status` and operation alias
`status` use the same connectivity decision and result schema.

| Operation | Released JSON fields | Validation |
| --- | --- | --- |
| `connectivity` | `reachable` | Boolean |
| `os_runtime` | `kernel_family`, `architecture`, `uptime_seconds` | `linux`; `x86_64`/`aarch64`/`other`; integer 0..2^40 |
| `capacity` | `cpu_count`, `memory_total_bytes`, `memory_available_bytes` | Integer 1..65536 CPUs; integer bytes 0..2^60; available <= total |

Results carry `schema_version: 1`. Only exact field sets pass; duplicate JSON
keys, invalid UTF-8, unexpected types, extra fields, and results exceeding 4096
bytes fail closed. No raw output or partial prefix is returned. Errors use fixed
codes; domain/facade code emits no logs, progress messages, artifacts or resource
caches containing guest data. The MCP facade also rejects unknown tools and
extra/non-string arguments before SDK error formatting, without echoing caller
data. Only three tools are registered; no resources or prompts export data.

Confidential mode explicitly denies screenshot, OCR, screen text, clipboard,
download, file read/export, video and shell/SSH/QMP passthrough requests at the
domain boundary. Unknown/unclassified operations deny in both modes. Content
exports and generic commands remain unsupported in standard mode as well.
Capabilities return fixed labels and no guest list or adapter handles. Agent
instructions forbid harvesting or encoding guest content, alternate transport
probes, and retries of denials; denial requires an operator decision.

## Transport gate and residual limits

There is no production transport implementation in this milestone. The tests
inject fakes only. A future adapter must map diagnostic enums to fixed,
reviewed executables and arguments, bind the private handle to a verified target,
control its environment, enforce the requested five-second execution timeout and
4096-byte collection cap, and avoid raw command/error logging. The service
independently validates the returned byte cap and release schema. It cannot
enforce an uncooperative adapter's execution deadline or memory use. Fake tests
verify the bound arguments and failure handling, not real process termination.

An eventual SSH adapter belongs behind this interface and requires explicit
operator provisioning, least-privilege command authority and verified host keys;
no credentials or host-key bypass are introduced here. Live integration requires
its own review and testing. Broader command exceptions and optional UI interaction
are intentionally still unspecified rather than guessed from incomplete scope.

Allowlisted metadata can itself be sensitive, and a malicious guest could encode
information in valid numeric or boolean results. This service is not a sandbox,
an egress control, protection from malicious operator Python code, or permission
to access private data. Other tools/MCP servers are outside its enforcement.
Arbitrary remote commands can disclose data through output or side effects;
generic regex redaction, truncation and harsh agent guidance cannot make them
confidential. This profile must not be represented as complete data-loss
prevention or an operational VM security guarantee.

Tests in `tests/test_vm_diagnostics.py` exercise fake diagnostics, both profiles,
direct domain/facade calls, MCP calls and aliases, authority denial, injection,
policy mutation, output caps/schema failures and synthetic secret canaries.
