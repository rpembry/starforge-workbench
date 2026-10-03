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
    practical_rename = True

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

    def rename_practical(self, identity, desired):
        self.writes.append((identity, None, desired))
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
    retried = service.apply(plan['plan_id'], all_eligible=True)['results']
    assert retried['alpha']['status'] == 'applied'
    assert retried['beta']['status'] == 'conflict'
    assert retried['beta']['reason'] == 'unresolved_provider_outcome'
    assert len(cat.writes) == 3
    assert service.preview(['beta'])['rows'][0]['reason'] == 'unresolved_provider_outcome'


def test_definitive_conflict_never_owns_racing_manual_title(fixture):
    service, cat, *_ = fixture
    plan = service.preview(['alpha'])
    def conflict(identity, expected, desired):
        cat.rows[identity]['title'] = desired  # another writer, not Workbench
        return 'conflict'
    cat.rename_if_title = conflict
    result = service.apply(plan['plan_id'], selected=['alpha'])
    assert result['results']['alpha']['status'] == 'conflict'
    assert 'alpha' not in result.get('verified_writes', {})
    assert service.undo(plan['plan_id'], 'alpha')['undo_results']['alpha']['reason'] == 'no_verified_write'


def test_each_row_is_refreshed_immediately_before_write(fixture):
    service, cat, contexts, *_ = fixture
    plan = service.preview(['alpha', 'beta'])
    original = cat.rename_if_title
    def change_second_while_first_writes(identity, expected, desired):
        if identity == A:
            contexts[1]['title'] = 'Changed by operator'
        return original(identity, expected, desired)
    cat.rename_if_title = change_second_while_first_writes
    results = service.apply(plan['plan_id'], all_eligible=True)['results']
    assert results['alpha']['status'] == 'applied'
    assert results['beta']['status'] == 'conflict'
    assert len(cat.writes) == 1


def test_removed_unselected_context_does_not_abort_selected_row(fixture):
    service, cat, contexts, *_ = fixture
    plan = service.preview(['alpha', 'beta'])
    contexts.pop()
    result = service.apply(plan['plan_id'], selected=['alpha'])
    assert result['results']['alpha']['status'] == 'applied'


def test_repeated_status_keeps_write_evidence_for_undo(fixture):
    service, cat, *_ = fixture
    cat.rows[A]['title'] = 'Prior'
    plan = service.preview(['alpha'])
    assert service.apply(plan['plan_id'], selected=['alpha'])['results']['alpha']['status'] == 'applied'
    for _ in range(2):
        assert service.apply(plan['plan_id'], selected=['alpha'])['results']['alpha']['status'] == 'already_matched'
    assert service.undo(plan['plan_id'], 'alpha')['undo_results']['alpha']['status'] == 'applied'


def test_practical_mode_requires_confirmation_and_reports_verified_write(fixture):
    service, cat, *_ = fixture
    cat.guarded_rename = False
    plan = service.preview(['alpha'])
    with pytest.raises(ValueError):
        service.apply(plan['plan_id'], selected=['alpha'], mode='practical')
    result = service.apply(plan['plan_id'], selected=['alpha'], mode='practical', confirm_non_atomic=True)
    assert result['results']['alpha']['status'] == 'applied'
    assert result['verified_writes']['alpha']['mode'] == 'practical'


def test_delayed_unknown_request_blocks_retry_and_undo(fixture):
    service, cat, *_ = fixture
    cat.rows[A]['title'] = 'Prior'
    plan = service.preview(['alpha'])
    def timeout_after_send(identity, expected, desired):
        cat.writes.append((identity, expected, desired))
        raise TimeoutError()
    cat.rename_if_title = timeout_after_send
    assert service.apply(plan['plan_id'], selected=['alpha'])['results']['alpha']['status'] == 'unknown'
    assert service.apply(plan['plan_id'], selected=['alpha'])['results']['alpha']['status'] == 'conflict'
    assert service.undo(plan['plan_id'], 'alpha')['undo_results']['alpha']['status'] == 'conflict'
    assert len(cat.writes) == 1


def test_real_catalog_write_capability_is_explicitly_blocked(tmp_path):
    assert CodexCatalog(tmp_path).guarded_rename is False
    with pytest.raises(NotImplementedError):
        CodexCatalog(tmp_path).rename_if_title(A, '', 'Alpha')


def test_supported_app_server_rename_request_uses_exact_id(tmp_path):
    fake = tmp_path / 'fake-codex'
    fake.write_text('''#!/usr/bin/env python3
import json, sys
first=json.loads(sys.stdin.readline())
assert first['method']=='initialize'
print(json.dumps({'id':first['id'],'result':{}}), flush=True)
assert json.loads(sys.stdin.readline())['method']=='initialized'
second=json.loads(sys.stdin.readline())
assert second['method']=='thread/name/set'
assert second['params']=={'threadId':'11111111-1111-4111-8111-111111111111','name':'Alpha'}
print(json.dumps({'id':second['id'],'result':{}}), flush=True)
''')
    fake.chmod(0o700)
    assert CodexCatalog(tmp_path, str(fake)).rename_practical(A, 'Alpha') == 'applied'


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
