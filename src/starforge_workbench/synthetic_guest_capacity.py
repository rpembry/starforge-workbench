"""Owner-local numeric input for the single stopped synthetic guest/Qwen trial.

This consumer validates and copies data, not guest authenticity or freshness.
The trusted VM owner supplies the fresh success envelope and cleanup evidence;
no caller can provide targets, prompts, commands, endpoints or raw guest logs.
No MCP/coordinator entry point or automatic release is registered here.
"""
from dataclasses import dataclass

from workbench.vm_diagnostics import Diagnostic, GuestRegistration

from .local_qwen_synthetic import LocalModelError, LocalQwen8BAdapter

GUEST_ID = 'g_' + '3' * 32
TRANSPORT_REF = 'synthetic-console'
GUEST_MEMORY_MAX = 768 * 1024**2
PLANNING_CPU_SLOTS = 2
SOURCE = 'local_synthetic_guest_capacity.v1'


@dataclass(frozen=True, slots=True)
class SyntheticGuestCapacity:
    """Immutable copied observation; cleanup booleans are trusted owner evidence."""
    cpu_count: int
    memory_total_bytes: int
    memory_available_bytes: int
    stopped: bool
    reaped: bool

    def __post_init__(self):
        if (type(self.cpu_count) is not int or self.cpu_count != 1
                or type(self.memory_total_bytes) is not int
                or not 0 < self.memory_total_bytes <= GUEST_MEMORY_MAX
                or type(self.memory_available_bytes) is not int
                or not 0 <= self.memory_available_bytes <= self.memory_total_bytes
                or type(self.stopped) is not bool or self.stopped is not True
                or type(self.reaped) is not bool or self.reaped is not True):
            raise LocalModelError()

    def numeric_metadata(self):
        return {'cpu_count': self.cpu_count, 'memory_total_bytes': self.memory_total_bytes,
                'memory_available_bytes': self.memory_available_bytes}


def consume_capacity(registration, envelope, *, stopped, reaped):
    """Trusted local owner only, after this exact synthetic guest is stopped/reaped."""
    if (type(registration) is not GuestRegistration or registration.guest_id != GUEST_ID
            or registration.transport_ref != TRANSPORT_REF
            or registration.allowed != frozenset({Diagnostic.CAPACITY})
            or type(envelope) is not dict
            or set(envelope) != {'schema_version', 'status', 'diagnostic', 'data'}
            or type(envelope['schema_version']) is not int or envelope['schema_version'] != 1
            or type(envelope['status']) is not str or envelope['status'] != 'ok'
            or type(envelope['diagnostic']) is not str or envelope['diagnostic'] != 'capacity'
            or type(envelope['data']) is not dict
            or set(envelope['data']) != {'cpu_count', 'memory_total_bytes', 'memory_available_bytes'}):
        raise LocalModelError()
    return SyntheticGuestCapacity(**envelope['data'], stopped=stopped, reaped=reaped)


class GuestCapacityQwenAdapter(LocalQwen8BAdapter):
    """Installed-model bounds inherited; only immutable numeric guest input added."""
    def __init__(self, runner, model, *, inference_slot, capacity):
        if type(capacity) is not SyntheticGuestCapacity:
            raise LocalModelError()
        self.capacity = capacity
        super().__init__(runner, model, inference_slot=inference_slot)

    def _prompt(self):
        metadata = self.capacity.numeric_metadata()
        return (
            '<|im_start|>system\nReturn only JSON with summary (string) and findings '
            '(array of strings). No tools. This is a synthetic planning exercise using '
            'a stopped synthetic guest observation. CPU count is not production worker capacity. '
            'Use the explicit assumption of one logical CPU per planning slot. '
            'Report a planning shortfall under that assumption only; do not infer actual '
            'worker capacity, throughput or a production configuration recommendation. '
            'Memory numbers are observation metadata only.\n<|im_end|>\n'
            '<|im_start|>user\nFixed synthetic planning demand: requested_logical_cpu_slots=2. '
            f'Observed guest: reported_logical_cpus={metadata["cpu_count"]}; '
            f'memory_total_bytes={metadata["memory_total_bytes"]}; '
            f'memory_available_bytes={metadata["memory_available_bytes"]}. '
            'Calculate the planning shortfall under the stated one-logical-CPU-per-slot '
            'assumption. State that this does not establish production worker capacity.\n<|im_end|>\n'
            '<|im_start|>assistant\n<think>\n\n</think>\n\n'
        )

    def analyze(self, fixture):
        self.evidence = None
        report = super().analyze(fixture)
        if type(self.evidence) is not dict:
            raise LocalModelError()
        self.evidence = {**self.evidence, 'source': SOURCE,
                         'guest_capacity': self.capacity.numeric_metadata(),
                         'synthetic_planning_cpu_slots': PLANNING_CPU_SLOTS,
                         'planning_assumption': 'one_logical_cpu_per_slot',
                         'guest_stopped': True, 'guest_reaped': True}
        return report
