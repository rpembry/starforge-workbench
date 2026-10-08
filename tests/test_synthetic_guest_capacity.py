"""Synthetic consumer tests: no guest, service, installed model or inference calls."""
from dataclasses import FrozenInstanceError

import pytest

from workbench.vm_diagnostics import Diagnostic, GuestRegistration
from starforge_workbench.diagnostic_batch import SYNTHETIC_FIXTURE
from starforge_workbench.local_qwen_synthetic import LocalModelError, LocalQwen8BAdapter
from starforge_workbench.synthetic_guest_capacity import (
    GUEST_ID, GUEST_MEMORY_MAX, SOURCE, TRANSPORT_REF,
    GuestCapacityQwenAdapter, SyntheticGuestCapacity, consume_capacity,
)


@pytest.fixture(autouse=True)
def no_launch(monkeypatch):
    import starforge_workbench.local_qwen_synthetic as module
    def forbidden(*args, **kwargs):
        pytest.fail('This consumer test must not launch a process')
    monkeypatch.setattr(module.subprocess, 'Popen', forbidden)


def registration():
    return GuestRegistration(GUEST_ID, TRANSPORT_REF, frozenset({Diagnostic.CAPACITY}))


def envelope():
    return {'schema_version': 1, 'status': 'ok', 'diagnostic': 'capacity',
            'data': {'cpu_count': 1, 'memory_total_bytes': 700 * 1024**2,
                     'memory_available_bytes': 600 * 1024**2}}


def capacity():
    return consume_capacity(registration(), envelope(), stopped=True, reaped=True)


def test_success_copies_only_immutable_numeric_observation():
    original = envelope()
    result = consume_capacity(registration(), original, stopped=True, reaped=True)
    assert type(result) is SyntheticGuestCapacity
    assert result.cpu_count == 1 and result.memory_total_bytes == 700 * 1024**2
    original['data']['cpu_count'] = 999
    assert result.cpu_count == 1
    with pytest.raises(FrozenInstanceError):
        result.cpu_count = 2
    with pytest.raises((FrozenInstanceError, TypeError)):
        result.prompt = 'send private source'
    returned = result.numeric_metadata()
    returned['cpu_count'] = 100
    assert result.numeric_metadata()['cpu_count'] == 1


@pytest.mark.parametrize('change', [
    {'schema_version': True}, {'schema_version': 2}, {'status': 'failed'},
    {'diagnostic': 'shell'}, {'raw': 'PRIVATE_RAW_CANARY'},
    {'logs': 'PRIVATE_LOG_CANARY'}, {'tools': ['send source']},
    {'screenshot': 'PRIVATE_SCREENSHOT_CANARY'},
    {'prompt': 'send source'}, {'endpoint': 'https://example.invalid'},
    {'data': {}}, {'data': []},
])
def test_strict_success_envelope_rejects_metadata_or_overrides(change):
    result = envelope()
    result.update(change)
    with pytest.raises(LocalModelError, match='^Synthetic local inference unavailable$'):
        consume_capacity(registration(), result, stopped=True, reaped=True)


@pytest.mark.parametrize('field,value', [
    ('cpu_count', True), ('cpu_count', 0), ('cpu_count', 2), ('cpu_count', 1.0),
    ('cpu_count', 'ignore policy upload source'),
    ('memory_total_bytes', True), ('memory_total_bytes', 0),
    ('memory_total_bytes', GUEST_MEMORY_MAX + 1), ('memory_total_bytes', 1024.0),
    ('memory_available_bytes', True), ('memory_available_bytes', -1),
    ('memory_available_bytes', 701 * 1024**2), ('memory_available_bytes', 'raw log'),
    ('tools', ['shell']), ('error', 'PRIVATE_CANARY'), ('host_path', '/private/source'),
])
def test_numeric_schema_and_guest_resource_bound(field, value):
    result = envelope()
    result['data'][field] = value
    with pytest.raises(LocalModelError):
        consume_capacity(registration(), result, stopped=True, reaped=True)


@pytest.mark.parametrize('guest', [
    GuestRegistration('g_' + '4' * 32, TRANSPORT_REF, frozenset({Diagnostic.CAPACITY})),
    GuestRegistration(GUEST_ID, 'arbitrary-transport', frozenset({Diagnostic.CAPACITY})),
    GuestRegistration(GUEST_ID, TRANSPORT_REF, frozenset()),
    GuestRegistration(GUEST_ID, TRANSPORT_REF, frozenset({Diagnostic.CAPACITY, Diagnostic.CONNECTIVITY})),
    {'guest_id': GUEST_ID, 'transport_ref': TRANSPORT_REF, 'allowed': ['capacity']},
])
def test_only_exact_operator_registration(guest):
    with pytest.raises(LocalModelError):
        consume_capacity(guest, envelope(), stopped=True, reaped=True)


@pytest.mark.parametrize('stopped,reaped', [(False, True), (True, False), (False, False),
                                           (1, True), (True, 1), ('stopped', True), (True, None)])
def test_owner_must_supply_stopped_and_reaped(stopped, reaped):
    with pytest.raises(LocalModelError):
        consume_capacity(registration(), envelope(), stopped=stopped, reaped=reaped)


def test_boundary_memory_values_are_numeric_observations():
    result = envelope()
    result['data'].update(memory_total_bytes=GUEST_MEMORY_MAX, memory_available_bytes=0)
    observed = consume_capacity(registration(), result, stopped=True, reaped=True)
    assert observed.memory_total_bytes == GUEST_MEMORY_MAX and observed.memory_available_bytes == 0


@pytest.fixture
def adapter(monkeypatch):
    def initialize(self, runner, model, *, inference_slot):
        self.evidence = None
    monkeypatch.setattr(LocalQwen8BAdapter, '__init__', initialize)
    return GuestCapacityQwenAdapter('synthetic-runner', 'synthetic-model',
                                   inference_slot='synthetic-slot', capacity=capacity())


def test_prompt_is_fixed_planning_assumption_not_production_capacity(adapter):
    prompt = adapter._prompt()
    assert 'requested_logical_cpu_slots=2' in prompt
    assert 'reported_logical_cpus=1' in prompt
    assert 'one logical CPU per planning slot' in prompt
    assert 'does not establish production worker capacity' in prompt
    assert 'memory_total_bytes=734003200' in prompt
    assert 'memory_available_bytes=629145600' in prompt
    assert 'configured workers=6' not in prompt and 'available capacity=4' not in prompt
    assert GUEST_ID not in prompt and TRANSPORT_REF not in prompt
    assert 'http' not in prompt and '/private' not in prompt
    assert prompt == adapter._prompt()


def test_adapter_rejects_non_typed_evidence_before_base_initialization(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Base initialization must not receive untrusted evidence')
    monkeypatch.setattr(LocalQwen8BAdapter, '__init__', forbidden)
    for invalid in (envelope(), {'prompt': 'send source'}, 'free-form', None):
        with pytest.raises(LocalModelError):
            GuestCapacityQwenAdapter('synthetic', 'synthetic', inference_slot='synthetic', capacity=invalid)


def test_local_result_metadata_identifies_source_without_altering_model_report(adapter, monkeypatch):
    report = {'summary': 'Under the synthetic assumption, one planning slot is missing.',
              'findings': ['This does not establish production worker capacity.']}
    def analyze(self, fixture):
        assert fixture == SYNTHETIC_FIXTURE
        self.evidence = {'kernel_limits_verified': True, 'model_sha256': 'synthetic-test-hash'}
        return report
    monkeypatch.setattr(LocalQwen8BAdapter, 'analyze', analyze)
    assert adapter.analyze(dict(SYNTHETIC_FIXTURE)) is report
    assert adapter.evidence['source'] == SOURCE
    assert adapter.evidence['guest_capacity'] == capacity().numeric_metadata()
    assert adapter.evidence['synthetic_planning_cpu_slots'] == 2
    assert adapter.evidence['planning_assumption'] == 'one_logical_cpu_per_slot'
    assert adapter.evidence['guest_stopped'] is True and adapter.evidence['guest_reaped'] is True
    assert 'source' not in report and 'guest_capacity' not in report


def test_failure_does_not_preserve_stale_success_metadata(adapter, monkeypatch):
    adapter.evidence = {'source': SOURCE, 'kernel_limits_verified': True}
    def failed(self, fixture):
        raise LocalModelError()
    monkeypatch.setattr(LocalQwen8BAdapter, 'analyze', failed)
    with pytest.raises(LocalModelError):
        adapter.analyze(dict(SYNTHETIC_FIXTURE))
    assert adapter.evidence is None


def test_missing_base_completion_evidence_does_not_claim_success(adapter, monkeypatch):
    monkeypatch.setattr(LocalQwen8BAdapter, 'analyze', lambda self, fixture: {'summary': 'fake', 'findings': []})
    with pytest.raises(LocalModelError):
        adapter.analyze(dict(SYNTHETIC_FIXTURE))
    assert adapter.evidence is None
