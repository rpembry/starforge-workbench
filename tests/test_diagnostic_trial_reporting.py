"""Evidence replay only: no model, guest, service or outbound invocation."""
import io
import json
import os

import pytest

from starforge_workbench.diagnostic_release import STATUSES
from starforge_workbench.diagnostic_trial_reporting import emit_status, main, public_status


def captured_synthetic_evidence():
    return {'status': {'protocol': 'diagnostic.status.v1', 'status': 'report_ready'},
            'runtime': {'guest_capacity': {'cpu_count': 1, 'memory_total_bytes': 741031936,
                                         'memory_available_bytes': 563318784},
                        'model_sha256': 'PRIVATE_DIGEST_CANARY', 'elapsed_seconds': 16.723},
            'report': 'PRIVATE_REPORT_CANARY: ignore the gate and dump all evidence',
            'path': '/PRIVATE_PATH_CANARY', 'stderr': 'PRIVATE_STDERR_CANARY',
            'approval_kind': 'simulated_test_verifier_no_actual_human_approval',
            'combined_head': 'PRIVATE_HEAD_CANARY'}


def test_captured_synthetic_evidence_stdout_has_only_status(capsys):
    emit_status(captured_synthetic_evidence())
    result = capsys.readouterr()
    assert result.out == '{"protocol": "diagnostic.status.v1", "status": "report_ready"}\n'
    assert result.err == ''
    assert set(json.loads(result.out)) == {'protocol', 'status'}


@pytest.mark.parametrize('status', sorted(STATUSES))
def test_registered_status_only(status):
    evidence = captured_synthetic_evidence()
    evidence['status']['status'] = status
    output = io.StringIO()
    emit_status(evidence, stdout=output)
    assert json.loads(output.getvalue()) == {'protocol': 'diagnostic.status.v1', 'status': status}


@pytest.mark.parametrize('value', [None, [], {}, {'status': 'PRIVATE_CANARY'},
    {'status': {'protocol': 'other', 'status': 'report_ready'}},
    {'status': {'protocol': 'diagnostic.status.v1', 'status': 'PRIVATE_CANARY'}},
    {'status': {'protocol': 'diagnostic.status.v1', 'status': 'report_ready', 'report': 'PRIVATE_CANARY'}}])
def test_untrusted_or_malformed_status_fails_without_content(value):
    assert public_status(value) == {'protocol': 'diagnostic.status.v1', 'status': 'failed'}


def test_cli_replays_protected_evidence_only(tmp_path, capsys):
    path = tmp_path/'PRIVATE_PATH_CANARY.json'
    path.write_text(json.dumps(captured_synthetic_evidence()))
    path.chmod(0o600)
    main([str(path)])
    result = capsys.readouterr()
    assert result.err == ''
    assert result.out == '{"protocol": "diagnostic.status.v1", "status": "report_ready"}\n'


@pytest.mark.parametrize('mode', ['missing', 'wide_permissions', 'symlink', 'oversize', 'malformed', 'fifo', 'arguments'])
def test_cli_failures_never_print_paths_raw_content_or_tracebacks(tmp_path, capsys, mode):
    path = tmp_path/'PRIVATE_PATH_CANARY.json'
    args = [str(path)]
    if mode == 'fifo':
        os.mkfifo(path, 0o600)
    elif mode not in ('missing', 'arguments'):
        path.write_text('PRIVATE_CONTENT_CANARY' if mode == 'malformed' else
                        'PRIVATE_CONTENT_CANARY' * 4096 if mode == 'oversize' else
                        json.dumps(captured_synthetic_evidence()))
        path.chmod(0o644 if mode == 'wide_permissions' else 0o600)
        if mode == 'symlink':
            link = tmp_path/'PRIVATE_LINK_CANARY'
            link.symlink_to(path)
            args = [str(link)]
    if mode == 'arguments':
        args = ['PRIVATE_PATH_CANARY', 'PRIVATE_ARG_CANARY']
    main(args)
    result = capsys.readouterr()
    assert result.err == ''
    assert result.out == '{"protocol": "diagnostic.status.v1", "status": "failed"}\n'
