"""Synthetic-first VM diagnostic boundary; no live transport is supplied.

Trusted operator code owns registrations and transport construction. This
policy constrains this service's dispatch and release, not arbitrary Python,
other interfaces, network egress, or a malicious guest's covert channels.
"""
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Protocol


class DisclosureProfile(str, Enum):
    CONFIDENTIAL = 'confidential'
    STANDARD = 'standard'


class Diagnostic(str, Enum):
    CONNECTIVITY = 'connectivity'
    OS_RUNTIME = 'os_runtime'
    CAPACITY = 'capacity'


GUEST_ID = re.compile(r'g_[0-9a-f]{32}')
MAX_OUTPUT_BYTES = 4096
TIMEOUT_SECONDS = 5
# An alias is a fixed mapping to the same canonical authorization decision.
ALIASES = MappingProxyType({'status': Diagnostic.CONNECTIVITY})
EXPORTS = frozenset({'screenshot', 'capture', 'ocr', 'screen_text',
                     'clipboard_read', 'download', 'file_read', 'export',
                     'video', 'shell', 'ssh', 'qmp'})


@dataclass(frozen=True, slots=True)
class StartupPolicy:
    profile: DisclosureProfile = DisclosureProfile.CONFIDENTIAL

    def __post_init__(self):
        if type(self.profile) is not DisclosureProfile:
            raise ValueError('Invalid disclosure profile')


@dataclass(frozen=True, slots=True)
class GuestRegistration:
    """Operator-owned authority, never accepted as a tool argument.

transport_ref is an opaque local adapter handle, not a caller-provided host,
path or SSH option. A future adapter must bind it to a verified guest target.
"""
    guest_id: str
    transport_ref: str = field(repr=False)
    allowed: frozenset[Diagnostic]

    def __post_init__(self):
        if (type(self.guest_id) is not str or not GUEST_ID.fullmatch(self.guest_id)
                or type(self.transport_ref) is not str or not self.transport_ref
                or type(self.allowed) is not frozenset
                or any(type(op) is not Diagnostic for op in self.allowed)):
            raise ValueError('Invalid guest registration')


class DiagnosticTransport(Protocol):
    def run(self, guest: GuestRegistration, operation: Diagnostic, *,
            timeout_seconds: int, max_output_bytes: int) -> bytes:
        """Execute a fixed diagnostic and return its JSON schema as bytes.

        Adapter MUST enforce timeout, resource and output collection bounds,
        fixed executable/arguments, controlled environment and no raw logs.
        It must not accept shell strings or route through generic passthrough.
        Release validation below is independent of those collection bounds.
        """


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Invalid diagnostic result')
        result[key] = value
    return result


def _integer(value, maximum):
    return type(value) is int and 0 <= value <= maximum


def _decode(operation: Diagnostic, raw: bytes) -> dict:
    if type(raw) is not bytes or len(raw) > MAX_OUTPUT_BYTES:
        raise ValueError('Invalid diagnostic result')
    data = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object)
    if type(data) is not dict:
        raise ValueError('Invalid diagnostic result')
    if operation is Diagnostic.CONNECTIVITY:
        valid = set(data) == {'reachable'} and type(data['reachable']) is bool
    elif operation is Diagnostic.OS_RUNTIME:
        valid = (set(data) == {'kernel_family', 'architecture', 'uptime_seconds'}
                 and data['kernel_family'] == 'linux'
                 and data['architecture'] in ('x86_64', 'aarch64', 'other')
                 and _integer(data['uptime_seconds'], 2**40))
    elif operation is Diagnostic.CAPACITY:
        valid = (set(data) == {'cpu_count', 'memory_total_bytes', 'memory_available_bytes'}
                 and _integer(data['cpu_count'], 65536) and data['cpu_count'] > 0
                 and _integer(data['memory_total_bytes'], 2**60)
                 and _integer(data['memory_available_bytes'], data['memory_total_bytes']))
    else:
        valid = False
    if not valid:
        raise ValueError('Invalid diagnostic result')
    return data


@dataclass(frozen=True, slots=True)
class DiagnosticService:
    policy: StartupPolicy = field(default_factory=StartupPolicy)
    registrations: tuple[GuestRegistration, ...] = ()
    transport: DiagnosticTransport | None = field(default=None, repr=False)

    def __post_init__(self):
        if (type(self.policy) is not StartupPolicy
                or type(self.registrations) is not tuple
                or any(type(guest) is not GuestRegistration for guest in self.registrations)
                or len({guest.guest_id for guest in self.registrations}) != len(self.registrations)):
            raise ValueError('Invalid VM service configuration')

    def capabilities(self) -> dict:
        return {'schema_version': 1, 'profile': self.policy.profile.value,
                'integration': 'unavailable' if self.transport is None else 'operator-supplied',
                'diagnostics': [op.value for op in Diagnostic],
                'content_exports': 'unsupported', 'arbitrary_commands': 'unsupported'}

    def execute(self, guest_id: str, operation: str) -> dict:
        """Canonical policy boundary for MCP, aliases, and direct calls."""
        def denied(reason):
            return {'schema_version': 1, 'status': 'denied', 'reason': reason}

        if type(operation) is not str:
            return denied('invalid_request')
        if operation in EXPORTS and self.policy.profile is DisclosureProfile.CONFIDENTIAL:
            return denied('confidential_policy')
        try:
            canonical = ALIASES[operation] if operation in ALIASES else Diagnostic(operation)
        except ValueError:
            return denied('unsupported_operation')
        if type(guest_id) is not str or not GUEST_ID.fullmatch(guest_id):
            return denied('invalid_request')
        guest = next((row for row in self.registrations if row.guest_id == guest_id), None)
        # This authority check is independent of disclosure profile.
        if guest is None or canonical not in guest.allowed:
            return denied('not_authorized')
        if self.transport is None:
            return {'schema_version': 1, 'status': 'unavailable', 'reason': 'transport_unavailable'}
        try:
            raw = self.transport.run(guest, canonical, timeout_seconds=TIMEOUT_SECONDS,
                                     max_output_bytes=MAX_OUTPUT_BYTES)
            data = _decode(canonical, raw)
        except Exception:
            # No raw exception, partial result, target, argument or stderr release.
            return {'schema_version': 1, 'status': 'failed', 'reason': 'diagnostic_failed'}
        return {'schema_version': 1, 'status': 'ok', 'diagnostic': canonical.value, 'data': data}
