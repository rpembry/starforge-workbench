import os
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from workbench.collector import collect, main
from workbench.client import client


def process(root, pid, parent, name, birth):
    p = root/str(pid)
    p.mkdir()
    fields = ['S', str(parent)] + ['0']*17 + [str(birth)]
    (p/'stat').write_text(f'{pid} ({name}) '+ ' '.join(fields))
    (p/'comm').write_text(name)


@pytest.mark.parametrize('provider', ['codex', 'opencode'])
def test_only_actual_provider_processes_become_runs(tmp_path, provider):
    proc = tmp_path/'proc'
    (proc/'sys/kernel/random').mkdir(parents=True)
    (proc/'sys/kernel/random/boot_id').write_text('synthetic-boot')
    (proc/'stat').write_text('btime 1000\n')
    process(proc, 10, 1, 'python', 10)
    process(proc, 20, 10, provider, 20)
    process(proc, 30, 1, 'python', 30)  # Claude launcher menu, no provider.
    manifest = tmp_path/'manifest.yaml'
    manifest.write_text(yaml.safe_dump({'contexts': [
        {'id': 'ai-workbench', 'provider': provider, 'enabled': True},
        {'id': 'claude-code', 'provider': 'claude', 'enabled': True},
    ]}))
    output = 'sfwb-ai-workbench|10|0|1100\nsfwb-claude-code|30|0|1100\n'
    with patch('workbench.collector.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=output)):
        first = collect(manifest, proc_root=proc)
        again = collect(manifest, proc_root=proc)
        assert len(first) == 1 and first[0]['provider'] == provider
        assert first[0]['source_id'] == again[0]['source_id']
        assert 'progress unknown' in first[0]['activity_basis']
        assert set(first[0]) == {'source', 'source_id', 'context', 'provider', 'actor', 'status', 'started_at', 'last_activity_at', 'activity_basis'}
        (proc/'sys/kernel/random/boot_id').write_text('another-boot')
        assert collect(manifest, proc_root=proc)[0]['source_id'] != first[0]['source_id']


def test_disabled_context_is_not_collected(tmp_path):
    proc = tmp_path/'proc'
    (proc/'sys/kernel/random').mkdir(parents=True)
    (proc/'sys/kernel/random/boot_id').write_text('synthetic-boot')
    (proc/'stat').write_text('btime 1000\n')
    process(proc, 10, 1, 'python', 10)
    process(proc, 20, 10, 'opencode', 20)
    manifest = tmp_path/'manifest.yaml'
    manifest.write_text(yaml.safe_dump({'contexts': [
        {'id': 'disabled', 'provider': 'opencode', 'enabled': False},
    ]}))
    output = 'sfwb-disabled|10|0|1100\n'
    with patch('workbench.collector.subprocess.run',
               return_value=SimpleNamespace(returncode=0, stdout=output)):
        assert collect(manifest, proc_root=proc) == []


def test_remote_plaintext_and_credential_urls_are_refused():
    for url in ['http://server.example.com:8027', 'https://user:password@example.com', 'https://example.com?token=secret']:
        with pytest.raises(ValueError):
            client(url, '/not-read')


def test_cli_passes_distinct_source_to_publisher_cycle(monkeypatch, capsys):
    observed = []

    def fake_cycle(api, manifest, selected, instance_id, **kwargs):
        observed.append((selected, kwargs['source']))
        return {'status': 'ok', 'reason': 'scan_complete', 'observed_runs': 0}

    monkeypatch.setattr('sys.argv', ['wb-collect', '--manifest', '/synthetic/manifest.yaml',
                                     '--context', 'opencode', '--source',
                                     'synthetic:opencode-instruction'])
    monkeypatch.setattr('workbench.collector.client', lambda *args: nullcontext(object()))
    monkeypatch.setattr('workbench.collector.cycle', fake_cycle)
    main()
    assert observed == [(['opencode'], 'synthetic:opencode-instruction')]
    assert '"status": "ok"' in capsys.readouterr().out
