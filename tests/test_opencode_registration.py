"""Synthetic local registration resolution; never reads a real launcher binding."""
import hashlib
import json
import socket

import yaml

from starforge_workbench import opencode_registration as resolver
from workbench.collector import _save_registration_state


REGISTERED = 'registered_synthetic_0001'
SESSION = 'ses_synthetic_session_001'


def fixture(tmp_path, monkeypatch):
    context = {'id': 'synthetic-opencode', 'enabled': True, 'provider': 'opencode',
               'title': 'Synthetic OpenCode', 'cwd': str(tmp_path)}
    manifest = tmp_path / 'manifest.yaml'
    manifest.write_text(yaml.safe_dump({'contexts': [context]}))
    state_dir = tmp_path / 'private-state'
    state_dir.mkdir(mode=0o700)
    state_file = state_dir / 'registrations.json'
    run = {'context': context['id'], 'source_id': 'synthetic-process-generation'}
    binding = {'id': SESSION, 'generation': 'a' * 64}
    generation = hashlib.sha256(json.dumps({
        'run': run['source_id'], 'binding': binding['generation'],
        'host': socket.gethostname(), 'display_name': context['title'],
        'provider': context['provider'],
    }, sort_keys=True).encode()).hexdigest()
    item = {'id': REGISTERED, 'generation': generation, 'sequence': 1,
            'host': socket.gethostname(), 'display_name': context['title'],
            'provider': 'opencode', 'run_id': 'synthetic-run-id',
            'action_id': None, 'last_activity_at': None}
    _save_registration_state(state_file, {'version': 1, 'contexts': {context['id']: item}})
    monkeypatch.setattr(resolver, 'collect', lambda *args: [run])
    monkeypatch.setattr(resolver, '_binding_record', lambda *args: binding)
    return manifest, state_file, state_dir, context, run, binding


def test_resolves_only_exact_live_registration(tmp_path, monkeypatch):
    manifest, state_file, state_dir, context, run, binding = fixture(tmp_path, monkeypatch)
    assert resolver.resolve_opencode_registration(
        REGISTERED, manifest, state_file, state_dir) == SESSION
    assert resolver.resolve_opencode_registration(
        'registered_other_synthetic', manifest, state_file, state_dir) is None
    assert resolver.resolve_opencode_registration(
        REGISTERED, manifest, state_file, state_dir, selected=('other',)) is None


def test_process_or_binding_replacement_fails_closed(tmp_path, monkeypatch):
    manifest, state_file, state_dir, context, run, binding = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(resolver, 'collect', lambda *args: [dict(run, source_id='replacement')])
    assert resolver.resolve_opencode_registration(REGISTERED, manifest, state_file, state_dir) is None
    monkeypatch.setattr(resolver, 'collect', lambda *args: [run])
    monkeypatch.setattr(resolver, '_binding_record',
                        lambda *args: dict(binding, generation='b' * 64))
    assert resolver.resolve_opencode_registration(REGISTERED, manifest, state_file, state_dir) is None
    monkeypatch.setattr(resolver, '_binding_record', lambda *args: None)
    assert resolver.resolve_opencode_registration(REGISTERED, manifest, state_file, state_dir) is None


def test_missing_provider_process_and_disabled_context_fail_closed(tmp_path, monkeypatch):
    manifest, state_file, state_dir, context, run, binding = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(resolver, 'collect', lambda *args: [])
    assert resolver.resolve_opencode_registration(REGISTERED, manifest, state_file, state_dir) is None
    context['enabled'] = False
    manifest.write_text(yaml.safe_dump({'contexts': [context]}))
    assert resolver.resolve_opencode_registration(REGISTERED, manifest, state_file, state_dir) is None
