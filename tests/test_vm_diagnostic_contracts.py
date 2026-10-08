from dataclasses import replace
import json

import pytest

from workbench.vm_diagnostics import Diagnostic
from workbench.vm_diagnostic_contracts import ContractError, LogWindow, log_counts, numeric_file


WINDOW = LogWindow('synthetic-worker.service', 100, 200)


def record(**changes):
    row = dict(service=WINDOW.service, timestamp=150, priority=3, message='SYNTHETIC_PRIVATE_LOG')
    row.update(changes)
    return json.dumps(row).encode()+b'\n'


def test_fixed_file_contract_never_releases_extra_fields_or_raw_errors():
    raw = b'{"cpu_count":1,"memory_total_bytes":100,"memory_available_bytes":80}'
    assert numeric_file(Diagnostic.CAPACITY, raw)['data']['cpu_count'] == 1
    for invalid in [raw[:-1]+b',"secret":"SYNTHETIC_CANARY"}', b'SYNTHETIC_CANARY',
                    b'{"cpu_count":1,"cpu_count":2}', b'x'*4097]:
        with pytest.raises(ContractError, match='^Local diagnostic contract unavailable$'):
            numeric_file(Diagnostic.CAPACITY, invalid)
    with pytest.raises(ContractError):
        numeric_file('capacity', raw)


def test_log_message_text_service_and_timestamp_are_not_returned():
    result = log_counts(WINDOW, record()+record(priority=6, message='synthetic second'))
    assert result['data'] == {'record_count':2, 'priority_counts':[0,0,0,1,0,0,1,0]}
    assert 'SYNTHETIC_PRIVATE_LOG' not in str(result) and WINDOW.service not in str(result)
    assert log_counts(WINDOW, b'')['data']['record_count'] == 0


@pytest.mark.parametrize('change', [{'service':'other.service'}, {'timestamp':99}, {'timestamp':201},
                                    {'timestamp':True}, {'priority':True}, {'priority':8},
                                    {'message':None}, {'message':'x'*1025}, {'extra':'SYNTHETIC_CANARY'}])
def test_log_scope_types_and_message_bounds_fail_closed(change):
    with pytest.raises(ContractError, match='^Local diagnostic contract unavailable$'):
        log_counts(WINDOW, record(**change))


def test_log_byte_and_line_caps_and_duplicate_keys():
    for raw in (b'x'*8193, record()*101, b'{"service":"x","service":"y"}\n', b'\xff'):
        with pytest.raises(ContractError):
            log_counts(WINDOW, raw)
    with pytest.raises(ContractError):
        log_counts(replace(WINDOW, max_lines=1), record()*2)
    with pytest.raises(ContractError):
        log_counts(replace(WINDOW, max_bytes=1), record())


@pytest.mark.parametrize('change', [{'service':'--all'}, {'service':'synthetic.service\n--all'},
                                    {'since':True}, {'until':401}, {'max_lines':101},
                                    {'max_bytes':8193}, {'max_lines':True}])
def test_window_configuration_has_fixed_scope_and_bounds(change):
    with pytest.raises(ContractError):
        replace(WINDOW, **change)


def test_journal_plan_is_argv_only_and_not_a_collection_or_launch():
    argv = WINDOW.journal_argv()
    assert '--unit=synthetic-worker.service' in argv and '--since=@100' in argv
    assert '--until=@200' in argv and '--lines=100' in argv
    assert '--all' not in argv and '--follow' not in argv
