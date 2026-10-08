"""All transports here are fakes; no VM, SSH, host configuration or data reads."""
import asyncio
from dataclasses import FrozenInstanceError
import json

from mcp import Client
import pytest

from workbench.vm_diagnostics import (
    ALIASES, EXPORTS, MAX_OUTPUT_BYTES, TIMEOUT_SECONDS, Diagnostic,
    DiagnosticService, DisclosureProfile, GuestRegistration, StartupPolicy,
)
from workbench.vm_mcp import build_server, main, startup_policy


GUEST = 'g_' + '1' * 32
OTHER_GUEST = 'g_' + '2' * 32
CANARY = 'SYNTHETIC_SECRET_DO_NOT_RELEASE'
RESULTS = {
    Diagnostic.CONNECTIVITY: {'reachable': True},
    Diagnostic.OS_RUNTIME: {'kernel_family': 'linux', 'architecture': 'x86_64', 'uptime_seconds': 42},
    Diagnostic.CAPACITY: {'cpu_count': 2, 'memory_total_bytes': 8192, 'memory_available_bytes': 4096},
}


class FakeTransport:
    def __init__(self, raw=None, error=None):
        self.raw = raw
        self.error = error
        self.calls = []

    def run(self, guest, operation, **bounds):
        self.calls.append((guest, operation, bounds))
        if self.error:
            raise self.error
        return self.raw if self.raw is not None else json.dumps(RESULTS[operation]).encode()


def service(transport=None, allowed=frozenset(Diagnostic), profile=DisclosureProfile.CONFIDENTIAL):
    return DiagnosticService(StartupPolicy(profile),
                             (GuestRegistration(GUEST, 'synthetic-local-handle', allowed),),
                             transport)


def invoke(server, name, arguments=None):
    async def run():
        async with Client(server) as client:
            return await client.call_tool(name, arguments or {})
    return asyncio.run(run())


@pytest.mark.parametrize('diagnostic', list(Diagnostic))
def test_useful_diagnostics_dispatch_fixed_operations_and_release_exact_schemas(diagnostic):
    transport = FakeTransport()
    result = service(transport).execute(GUEST, diagnostic.value)
    assert result == {'schema_version': 1, 'status': 'ok', 'diagnostic': diagnostic.value,
                      'data': RESULTS[diagnostic]}
    guest, dispatched, bounds = transport.calls[0]
    assert guest.guest_id == GUEST and dispatched is diagnostic
    assert bounds == {'timeout_seconds': TIMEOUT_SECONDS, 'max_output_bytes': MAX_OUTPUT_BYTES}


@pytest.mark.parametrize('profile', list(DisclosureProfile))
@pytest.mark.parametrize('operation', ['connectivity', 'status', 'os_runtime', 'capacity'])
def test_execution_authority_is_independent_of_mode_and_aliases(profile, operation):
    transport = FakeTransport()
    selected = service(transport, allowed=frozenset(), profile=profile)
    assert selected.execute(GUEST, operation)['reason'] == 'not_authorized'
    assert selected.execute(OTHER_GUEST, operation)['reason'] == 'not_authorized'
    assert not transport.calls


@pytest.mark.parametrize('operation', sorted(EXPORTS) + [
    'new_unclassified_tool', 'connectivity;cat /synthetic-secret',
    '$(cat /synthetic-secret)', 'CONNECTIVITY', 'status\nwhoami',
    'connectivity --output-file synthetic.json', 'set_profile', 'disable_confidential',
])
def test_direct_calls_fail_closed_before_transport(operation):
    transport = FakeTransport()
    result = service(transport).execute(GUEST, operation)
    assert result['status'] == 'denied'
    assert result['reason'] in {'confidential_policy', 'unsupported_operation'}
    assert not transport.calls


@pytest.mark.parametrize('guest_id', ['host.example.invalid', GUEST + ';id', '../guest', '', None])
def test_no_caller_host_path_or_argument_injection(guest_id):
    transport = FakeTransport()
    assert service(transport).execute(guest_id, 'connectivity')['reason'] == 'invalid_request'
    assert not transport.calls


@pytest.mark.parametrize('raw', [
    CANARY.encode(), json.dumps({'reachable': True, 'stdout': CANARY}).encode(),
    json.dumps({'reachable': CANARY}).encode(), b'{"reachable":1}', b'[]',
    b'{"reachable":true,"reachable":false}', b'{"reachable":NaN}',
    b'{"reachable":true}' + b' ' * MAX_OUTPUT_BYTES, b'\xff',
    {'reachable': True}, b'{}', b'{"reachable":true}\n' + CANARY.encode(),
])
def test_invalid_or_oversized_result_never_releases_raw_bytes(raw, capsys, caplog):
    result = service(FakeTransport(raw)).execute(GUEST, 'connectivity')
    assert result == {'schema_version': 1, 'status': 'failed', 'reason': 'diagnostic_failed'}
    assert CANARY not in json.dumps(result) + capsys.readouterr().out + caplog.text


@pytest.mark.parametrize('operation,data', [
    ('os_runtime', {'kernel_family': 'linux', 'architecture': CANARY, 'uptime_seconds': 0}),
    ('os_runtime', {'kernel_family': CANARY, 'architecture': 'x86_64', 'uptime_seconds': 0}),
    ('os_runtime', {'kernel_family': 'linux', 'architecture': 'x86_64', 'uptime_seconds': True}),
    ('os_runtime', {'kernel_family': 'linux', 'architecture': 'x86_64', 'uptime_seconds': -1}),
    ('capacity', {'cpu_count': True, 'memory_total_bytes': 2, 'memory_available_bytes': 1}),
    ('capacity', {'cpu_count': 0, 'memory_total_bytes': 2, 'memory_available_bytes': 1}),
    ('capacity', {'cpu_count': 2, 'memory_total_bytes': 2, 'memory_available_bytes': 3}),
    ('capacity', {'cpu_count': 2, 'memory_total_bytes': float('inf'), 'memory_available_bytes': 1}),
    ('capacity', {'cpu_count': 2, 'memory_total_bytes': 2**61, 'memory_available_bytes': 1}),
])
def test_all_diagnostic_schemas_fail_closed(operation, data):
    result = service(FakeTransport(json.dumps(data).encode())).execute(GUEST, operation)
    assert result['reason'] == 'diagnostic_failed'
    assert 'data' not in result


@pytest.mark.parametrize('error', [RuntimeError(CANARY), TimeoutError(CANARY)])
def test_adapter_failures_are_content_free(error, caplog, capsys):
    result = service(FakeTransport(error=error)).execute(GUEST, 'connectivity')
    assert result['reason'] == 'diagnostic_failed'
    captured = capsys.readouterr()
    assert CANARY not in json.dumps(result) + caplog.text + captured.out + captured.err


def test_policy_and_registration_are_immutable_after_startup(monkeypatch):
    selected = service(FakeTransport())
    monkeypatch.setenv('WB_VM_CONFIDENTIAL', '0')
    monkeypatch.setenv('WB_VM_PROFILE', 'standard')
    with pytest.raises(FrozenInstanceError):
        selected.policy.profile = DisclosureProfile.STANDARD
    with pytest.raises(FrozenInstanceError):
        selected.policy = StartupPolicy(DisclosureProfile.STANDARD)
    with pytest.raises(FrozenInstanceError):
        selected.registrations[0].allowed = frozenset()
    with pytest.raises(TypeError):
        ALIASES['screenshot'] = Diagnostic.CONNECTIVITY
    assert selected.execute(GUEST, 'screenshot')['reason'] == 'confidential_policy'


def test_invalid_operator_configuration_and_duplicate_ids_rejected():
    with pytest.raises(ValueError, match='Invalid disclosure profile'):
        StartupPolicy('standard')
    with pytest.raises(ValueError, match='Invalid guest registration'):
        GuestRegistration(GUEST, 'synthetic', {'connectivity'})
    guest = GuestRegistration(GUEST, 'synthetic', frozenset(Diagnostic))
    with pytest.raises(ValueError, match='Invalid VM service configuration'):
        DiagnosticService(registrations=(guest, guest))


def test_startup_default_explicit_modes_and_redacted_configuration_errors(capsys):
    assert startup_policy([]).profile is DisclosureProfile.CONFIDENTIAL
    assert startup_policy(['--confidential']).profile is DisclosureProfile.CONFIDENTIAL
    assert startup_policy(['--standard']).profile is DisclosureProfile.STANDARD
    for arguments in [['--confidential', '--standard'], [CANARY], ['--standard', CANARY]]:
        with pytest.raises(SystemExit) as error:
            startup_policy(arguments)
        assert str(error.value) == 'Invalid VM startup configuration'
    assert CANARY not in str(capsys.readouterr())


def test_cli_constructs_empty_service_and_stdio_only(monkeypatch):
    import workbench.vm_mcp as module
    seen = []
    class FakeServer:
        def run(self, **kwargs):
            seen.append(kwargs)
    def fake_build(selected):
        seen.append(selected)
        return FakeServer()
    monkeypatch.setattr(module, 'build_server', fake_build)
    main(['--confidential'])
    assert seen[0].policy.profile is DisclosureProfile.CONFIDENTIAL
    assert seen[0].registrations == () and seen[0].transport is None
    assert seen[1] == {'transport': 'stdio'}


def test_absent_integration_and_standard_never_enable_exports():
    assert DiagnosticService().capabilities()['integration'] == 'unavailable'
    assert service().execute(GUEST, 'connectivity')['reason'] == 'transport_unavailable'
    transport = FakeTransport()
    selected = service(transport, profile=DisclosureProfile.STANDARD)
    assert selected.execute(GUEST, 'shell')['reason'] == 'unsupported_operation'
    assert selected.execute(GUEST, 'screenshot')['reason'] == 'unsupported_operation'
    assert not transport.calls


def test_mcp_tools_and_alias_use_the_canonical_boundary():
    transport = FakeTransport()
    server = build_server(service(transport))
    result = invoke(server, 'vm_diagnostic', {'guest_id': GUEST, 'operation': 'connectivity'})
    assert result.structured_content['data'] == {'reachable': True}
    result = invoke(server, 'vm_status', {'guest_id': GUEST})
    assert result.structured_content['diagnostic'] == 'connectivity'
    assert len(transport.calls) == 2
    for op in ('screenshot', 'shell', 'status;id', 'disable_confidential'):
        result = invoke(server, 'vm_diagnostic', {'guest_id': GUEST, 'operation': op})
        assert result.structured_content['status'] == 'denied'
    denied_server = build_server(service(transport, allowed=frozenset()))
    assert invoke(denied_server, 'vm_status', {'guest_id': GUEST}).structured_content['reason'] == 'not_authorized'
    assert len(transport.calls) == 2


def test_mcp_unknown_tool_and_override_arguments_do_not_dispatch():
    transport = FakeTransport()
    server = build_server(service(transport))
    async def run():
        async with Client(server) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert names == {'vm_capabilities', 'vm_diagnostic', 'vm_status'}
            assert (await client.list_resources()).resources == []
            assert (await client.list_prompts()).prompts == []
            for name, args in [('screenshot', {'guest_id': GUEST}),
                               ('vm_diagnostic', {'guest_id': GUEST, 'operation': 'connectivity',
                                                  'profile': 'standard'})]:
                result = await client.call_tool(name, args)
                assert result.is_error
    asyncio.run(run())
    assert not transport.calls


def test_direct_facade_rejections_redact_caller_data(caplog, capsys):
    transport = FakeTransport()
    server = build_server(service(transport))
    async def run():
        for name, arguments in [(CANARY, {}), ('vm_diagnostic', {'guest_id': CANARY}),
                                ('vm_diagnostic', {'guest_id': GUEST, 'operation': 'connectivity',
                                                   CANARY: 'standard'}),
                                ('vm_diagnostic', {'guest_id': [CANARY], 'operation': 'connectivity'})]:
            result = await server.call_tool(name, arguments)
            assert result.is_error and CANARY not in str(result)
    asyncio.run(run())
    captured = capsys.readouterr()
    assert CANARY not in caplog.text + captured.out + captured.err
    assert not transport.calls


def test_mcp_failure_and_capabilities_do_not_disclose_canaries(caplog, capsys):
    server = build_server(service(FakeTransport(error=RuntimeError(CANARY))))
    failed = invoke(server, 'vm_diagnostic', {'guest_id': GUEST, 'operation': 'connectivity'})
    capabilities = invoke(server, 'vm_capabilities').structured_content
    assert failed.structured_content['reason'] == 'diagnostic_failed'
    assert capabilities['profile'] == 'confidential'
    assert GUEST not in json.dumps(capabilities)
    captured = capsys.readouterr()
    assert CANARY not in str(failed) + caplog.text + captured.out + captured.err
    assert 'On denial, stop' in server.instructions
    assert 'guidance cannot make arbitrary commands confidential' in server.instructions
