"""Synthetic exact-ID title reconciliation; never touches live Codex records."""
from copy import deepcopy
from pathlib import Path
import uuid

import pytest

from starforge_workbench.title_reconcile import CodexCatalog, Reconciler

A = str(uuid.UUID('11111111-1111-4111-8111-111111111111'))
B = str(uuid.UUID('22222222-2222-4222-8222-222222222222'))


class Catalog:
    guarded_rename = True

    def __init__(self):
        self.rows = {A: {'id': A, 'title': '', 'cwd': '/synthetic/a', 'archived': False, 'source': 'cli'},
                     B: {'id': B, 'title': '', 'cwd': '/synthetic/b', 'archived': False, 'source': 'cli'}}
        self.writes = []
        self.outcomes = {}

    def read(self, identity):
        return deepcopy(self.rows.get(identity))

    def rename_if_title(self, identity, expected, desired):
        self.writes.append((identity, expected, desired))
        outcome = self.outcomes.pop(identity, None)
        if outcome == 'timeout':
            raise TimeoutError()
        if outcome == 'blocked':
            raise PermissionError()
        if self.rows[identity]['title'] != expected:
            return 'conflict'
        self.rows[identity]['title'] = desired
        return 'applied'


@pytest.fixture
def fixture(tmp_path):
    cat = Catalog()
    contexts = [dict(id='alpha', provider='codex', title='Alpha ✨', cwd='/synthetic/a'),
                dict(id='beta', provider='codex', title='Beta', cwd='/synthetic/b')]
    bindings = {'alpha': dict(id=A, provider='codex', cwd='/synthetic/a', source_cwd='/synthetic/a'),
                'beta': dict(id=B, provider='codex', cwd='/synthetic/b', source_cwd='/synthetic/b')}
    observed = {}
    service = Reconciler(lambda: deepcopy(contexts), lambda identity: deepcopy(bindings.get(identity)),
                         cat, tmp_path / 'private', lambda context: observed.get(context['id']))
    return service, cat, contexts, bindings, observed


def test_preview_selected_apply_readback_and_repeat(fixture):
    service, cat, contexts, bindings, _ = fixture
    before_binding = deepcopy(bindings)
    before_contexts = deepcopy(contexts)
    before_records = deepcopy(cat.rows)
    plan = service.preview(['alpha', 'beta'])
    assert [r['status'] for r in plan['rows']] == ['eligible', 'eligible']
    assert cat.writes == []
    applied = service.apply(plan['plan_id'], selected=['alpha'])
    assert applied['results']['alpha']['status'] == 'applied'
    assert cat.rows[A]['title'] == 'Alpha ✨'
    assert cat.rows[B]['title'] == ''
    assert service.apply(plan['plan_id'], selected=['alpha'])['results']['alpha']['status'] == 'already_matched'
    assert len(cat.writes) == 1
    assert service.apply(plan['plan_id'], all_eligible=True)['results']['beta']['status'] == 'applied'
    assert len(cat.writes) == 2
    assert service.status(plan['plan_id'])['results']['beta']['status'] == 'applied'
    assert bindings == before_binding and contexts == before_contexts
    for identity in (A, B):
        assert {key: value for key, value in cat.rows[identity].items() if key != 'title'} == {
            key: value for key, value in before_records[identity].items() if key != 'title'}


def test_duplicate_labels_on_distinct_threads_are_not_alias_conflict(fixture):
    service, cat, contexts, *_ = fixture
    contexts[1]['title'] = contexts[0]['title']
    plan = service.preview(['alpha', 'beta'])
    assert [row['status'] for row in plan['rows']] == ['eligible', 'eligible']
    assert cat.writes == []


@pytest.mark.parametrize('mutation,reason', [
    (lambda c,b,o,k: c[0].update(title='Changed'), 'plan_changed'),
    (lambda c,b,o,k: b['alpha'].update(id=B), 'stale_binding'),
    (lambda c,b,o,k: k.rows[A].update(title='Manual'), 'title_changed'),
    (lambda c,b,o,k: o.update(alpha='Observed other'), 'label_disagreement'),
])
def test_rechecks_expected_state_and_identity(fixture, mutation, reason):
    service, cat, contexts, bindings, observed = fixture
    plan = service.preview(['alpha'])
    mutation(contexts, bindings, observed, cat)
    result = service.apply(plan['plan_id'], selected=['alpha'])['results']['alpha']
    assert result['status'] == 'conflict'
    assert result['reason'] in (reason, 'plan_changed', 'ambiguous_alias')
    assert cat.writes == []


def test_conflicts_and_custom_name_choice(fixture):
    service, cat, contexts, bindings, observed = fixture
    contexts[0]['title'] = ' '
    assert service.preview(['alpha'])['rows'][0]['reason'] == 'blank_label'
    contexts[0]['title'] = 'x' * 201
    assert service.preview(['alpha'])['rows'][0]['reason'] == 'invalid_label'
    contexts[0]['title'] = 'Alpha'
    cat.rows[A]['title'] = 'Custom'
    plan = service.preview(['alpha'])
    assert plan['rows'][0]['status'] == 'needs_choice'
    with pytest.raises(ValueError):
        service.apply(plan['plan_id'], all_eligible=True)
    assert service.apply(plan['plan_id'], selected=['alpha'])['results']['alpha']['status'] == 'applied'
    contexts[1]['title'] = 'Other'
    bindings['beta'] = dict(bindings['alpha'])
    assert [r['reason'] for r in service.preview(['alpha', 'beta'])['rows']] == ['ambiguous_alias', 'ambiguous_alias']
    assert service.preview(['alpha'])['rows'][0]['reason'] == 'ambiguous_alias'


def test_missing_archived_and_blocked_provider(fixture):
    service, cat, contexts, bindings, observed = fixture
    bindings.pop('alpha')
    assert service.preview(['alpha'])['rows'][0]['reason'] == 'missing_binding'
    bindings['alpha'] = dict(id=A, provider='codex', cwd='/synthetic/a')
    cat.rows[A]['archived'] = True
    assert service.preview(['alpha'])['rows'][0]['reason'] == 'missing_or_archived_thread'
    cat.rows[A]['archived'] = False
    cat.guarded_rename = False
    plan = service.preview(['alpha'])
    assert service.apply(plan['plan_id'], selected=['alpha'])['results']['alpha']['status'] == 'blocked'
    assert not cat.writes


def test_partial_failure_retry_and_uncertain_ack(fixture):
    service, cat, *_ = fixture
    plan = service.preview(['alpha', 'beta'])
    cat.outcomes[A] = 'blocked'
    cat.outcomes[B] = 'timeout'
    results = service.apply(plan['plan_id'], all_eligible=True)['results']
    assert results['alpha']['status'] == 'blocked'
    assert results['beta']['status'] == 'unknown'
    assert len(cat.writes) == 2
    assert service.apply(plan['plan_id'], all_eligible=True)['results']['alpha']['status'] == 'applied'
    assert len(cat.writes) == 4


def test_real_catalog_write_capability_is_explicitly_blocked(tmp_path):
    assert CodexCatalog(tmp_path).guarded_rename is False
    with pytest.raises(NotImplementedError):
        CodexCatalog(tmp_path).rename_if_title(A, '', 'Alpha')


def test_undo_requires_verified_write_and_unchanged_current_title(fixture):
    service, cat, *_ = fixture
    cat.rows[A]['title'] = 'Prior'
    plan = service.preview(['alpha'])
    assert plan['rows'][0]['status'] == 'needs_choice'
    assert service.apply(plan['plan_id'], selected=['alpha'])['results']['alpha']['status'] == 'applied'
    cat.rows[A]['title'] = 'Later manual edit'
    assert service.undo(plan['plan_id'], 'alpha')['undo_results']['alpha']['status'] == 'conflict'
    assert len(cat.writes) == 1
    cat.rows[A]['title'] = 'Alpha ✨'
    assert service.undo(plan['plan_id'], 'alpha')['undo_results']['alpha']['status'] == 'applied'
    assert cat.rows[A]['title'] == 'Prior'
