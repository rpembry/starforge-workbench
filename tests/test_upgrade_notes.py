import json
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs

import httpx
import pytest

from workbench import notifications as n
from workbench import upgrade_notes as u

NOW = 1800000000.0


@pytest.fixture
def config(tmp_path):
    return n.Settings(state_dir=str(tmp_path/'notifier'), min_interval_seconds=5,
                      upgrade_notes=u.UpgradeSettings(enabled=True, project_id='fixture_notifications',
                                                       token_file=str(tmp_path/'token')))


def event(app='codex_cli', before='1.2.2', after='1.2.3'):
    return u.UpgradeEvent(app_id=app, before_version=before, installed_version=after)


def record(config, **kwargs):
    key = u.record(config, event(**kwargs))['event_id']
    with n.locked_state(Path(config.state_dir)):
        state = u.read_state(config)
        u.import_inbox(config, state)
    return key


def lookup(e):
    return u.Notes(status='matched', highlights=['Synthetic fixture improvements'])


def sender(config, receipt):
    return 'fixture_task'


def prepared_receipt(config):
    receipt = u.UpgradeReceipt(event=event(), notes=lookup(event()))
    receipt.task_args = u.TaskArguments.model_validate(u.payload(config, receipt))
    return receipt


def token(config):
    p = Path(config.upgrade_notes.token_file)
    p.write_text('x'*40)
    p.chmod(0o600)


def test_disabled_has_no_state_network_or_credentials(tmp_path):
    config = n.Settings(state_dir=str(tmp_path/'absent'))
    assert u.record(config, {'invalid': 'ignored'}) == {'status': 'disabled'}
    assert u.poll(config, sender=Mock(side_effect=AssertionError())) == {'status': 'disabled'}
    assert u.status(config) == {'status': 'disabled'}
    assert not (tmp_path/'absent').exists()
    assert n.Settings().upgrade_notes is None


@pytest.mark.parametrize('update', [dict(installed_version='1.2.2'), dict(installed_version='../bad'),
    dict(app_id='other'), dict(schema_version=2), dict(notes_url='https://example.invalid'),
    dict(installed_version=True), dict(before_version='1.2\nprivate')])
def test_event_rejects_unknown_or_unverified_shape(update):
    with pytest.raises(ValueError):
        u.UpgradeEvent.model_validate({**event().model_dump(), **update})


def test_record_is_durable_no_network_and_dedupes_after_completion(config):
    key = record(config)
    assert Path(config.state_dir, 'upgrades.json').stat().st_mode & 0o777 == 0o600
    state = u.read_state(config)
    command = state.entries[key].command_id
    assert u.poll(config, lookup=lookup, sender=sender, current=NOW)['status'] == 'delivered'
    # Completion/deletion in Todoist is deliberately never queried.
    assert u.record(config, event(before='1.0.0'))['status'] == 'already_recorded'
    assert u.read_state(config).entries[key].command_id == command
    assert u.poll(config, sender=Mock(side_effect=AssertionError()), current=NOW+99)['status'] == 'idle'
    record(config, app='codex_desktop')
    record(config, after='1.2.4')
    assert len(u.read_state(config).entries) == 3


def test_first_event_bytes_are_preserved(config):
    key = record(config)
    assert u.record(config, event(before='1.0.0'))['status'] == 'already_recorded'
    assert u.read_state(config).entries[key].event.before_version == '1.2.2'


def test_destination_source_rebinding_fails_closed(config):
    record(config)
    for field, value in [('project_id', 'other'), ('source_id', 'other')]:
        altered = config.model_copy(deep=True)
        setattr(altered.upgrade_notes, field, value)
        with pytest.raises(n.NotificationError, match='upgrade_destination_mismatch'):
            u.poll(altered, sender=Mock(side_effect=AssertionError()))


def test_preview_does_not_create_state_or_read_token(config):
    assert u.poll(config, preview=True) == {'status': 'preview', 'pending': 0, 'inbox_pending': 0}
    assert not Path(config.state_dir).exists()
    record(config)
    assert u.poll(config, preview=True)['pending'] == 1


def test_lock_shared_with_attention(config):
    record(config)
    with n.locked_state(Path(config.state_dir)):
        with pytest.raises(n.NotificationError, match='notifier_already_running'):
            u.poll(config)


def test_intent_persisted_before_dispatch_and_crash_is_unknown(config):
    key = record(config)
    def crash(c, r):
        on_disk = json.loads(Path(c.state_dir, 'upgrades.json').read_text())['entries'][key]
        assert on_disk['outcome'] == 'sending'
        assert on_disk['command_id'] == r.command_id
        assert on_disk['notes'] == r.notes.model_dump()
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        u.poll(config, sender=crash, lookup=lookup, current=NOW)
    assert u.read_state(config).entries[key].outcome == 'unknown'
    assert u.poll(config, sender=Mock(side_effect=AssertionError()))['status'] == 'unknown'


def test_ack_persistence_failure_remains_unknown(config, monkeypatch):
    key = record(config)
    real_save = u.save
    def fail_ack(c, state):
        if state.entries[key].outcome == 'delivered':
            raise OSError('synthetic fsync failure')
        real_save(c, state)
    monkeypatch.setattr(u, 'save', fail_ack)
    with pytest.raises(OSError):
        u.poll(config, sender=sender, lookup=lookup, current=NOW)
    assert u.read_state(config).entries[key].outcome == 'unknown'


def test_safe_retry_keeps_uuid_payload_and_lookup(config):
    key = record(config)
    calls = []
    def fail(c, r):
        calls.append((r.command_id, r.temp_id, u.payload(c, r)))
        raise n.NotificationError('not_submitted', retryable=True)
    lookup_mock = Mock(side_effect=lookup)
    assert u.poll(config, sender=fail, lookup=lookup_mock, current=NOW)['status'] == 'retry_pending'
    assert u.poll(config, sender=fail, current=NOW+1)['status'] == 'idle'
    assert u.poll(config, sender=fail, current=NOW+5)['status'] == 'retry_pending'
    assert u.poll(config, sender=fail, current=NOW+10)['status'] == 'blocked'
    assert calls[0] == calls[1] == calls[2]
    assert lookup_mock.call_count == 1
    assert u.read_state(config).entries[key].failures == 3


@pytest.mark.parametrize('exception', [n.NotificationError('lost_ack', unknown=True), RuntimeError('PRIVATE body')])
def test_unknown_holds_all_upgrade_events_without_leaking(config, exception):
    record(config)
    record(config, after='1.2.4')
    def fail(c, r):
        raise exception
    result = u.poll(config, sender=fail, lookup=lookup, current=NOW)
    assert result['status'] == 'unknown'
    assert 'PRIVATE' not in json.dumps(result)
    assert u.poll(config, sender=Mock(side_effect=AssertionError()))['status'] == 'unknown'


def test_reconciliation_exact_ids_and_no_send(config):
    key = record(config)
    def uncertain(c, r):
        raise n.NotificationError('unknown', unknown=True)
    u.poll(config, sender=uncertain, lookup=lookup)
    command = u.status(config)['entries'][0]['command_id']
    for k, c, t in [('wrong', command, 'task'), (key, 'wrong', 'task'), (key, command, '../bad')]:
        with pytest.raises(n.NotificationError):
            u.reconcile(config, k, c, t)
    assert u.reconcile(config, key, command, 'confirmed_existing_task')['status'] == 'resolved_delivered'
    assert u.read_state(config).entries[key].task_id == 'confirmed_existing_task'
    assert u.poll(config, sender=Mock(side_effect=AssertionError()))['status'] == 'idle'


def test_quiet_hours_and_attention_state_preserved(config):
    record(config)
    attention = n.DeliveryState(scope=n.scope(config))
    attention.entries['a'*64] = n.Entry(fingerprint='b'*64)
    n.save_state(Path(config.state_dir), attention)
    u.poll(config, sender=sender, lookup=lookup)
    assert n.read_state(Path(config.state_dir), config).model_dump() == attention.model_dump()
    config.quiet_hours = n.QuietHours(timezone='UTC', start='00:00', end='23:59')
    assert u.poll(config, sender=Mock(side_effect=AssertionError()), current=NOW)['status'] == 'quiet_hours'


def fixture_get(body, status=200):
    return httpx.MockTransport(lambda request: httpx.Response(status, json=body) if isinstance(body, dict)
                               else httpx.Response(status, text=body))


def test_cli_official_tag_match_bounded_highlights():
    notes = u.lookup_notes(event(), fixture_get({'tag_name': 'rust-v1.2.3', 'body': '- First [link](https://example.invalid)\n- Second\n- Third\n- Fourth'}))
    assert notes.status == 'matched' and notes.highlights == ['First link', 'Second', 'Third']
    assert u.lookup_notes(event(), fixture_get({'tag_name': 'wrong', 'body': '- Private'})).status == 'lookup_failed'
    assert u.lookup_notes(event(), fixture_get('', 404)).status == 'not_found'
    assert u.lookup_notes(event(), fixture_get('', 500)).status == 'lookup_failed'
    assert u.lookup_notes(event(), fixture_get('', 302)).status == 'lookup_failed'
    assert u.lookup_notes(event(), fixture_get('x'*(u.MAX_SOURCE_BYTES+1))).status == 'lookup_failed'


def test_desktop_requires_exact_token_not_train_or_other_app():
    def page(heading, topics='codex-app'):
        return f'<li data-codex-topics="{topics}" id="fixture-entry"><h3>{heading}</h3><article><p>Fixture improvement.</p></article></li>'
    e = event(app='codex_desktop', after='2026.101.4')
    for h in ['App 2026.101', 'App 2026.101.45', 'App 2026.101.4.1', 'App 2026.101.4-beta', 'App 2026.101.4+build']:
        assert u.lookup_notes(e, fixture_get(page(h))).status == 'not_found'
    assert u.lookup_notes(e, fixture_get(page('App 2026.101.4', 'codex-cli'))).status == 'not_found'
    notes = u.lookup_notes(e, fixture_get(page('App 2026.101.4')))
    assert notes.status == 'matched' and notes.anchor == 'fixture-entry'
    assert u.lookup_notes(e, fixture_get('bad', 503)).status == 'lookup_failed'


def test_sync_transport_immutable_uuid_receipt_no_account_query(config):
    token(config)
    receipt = prepared_receipt(config)
    def handler(request):
        assert str(request.url) == u.SYNC_URL
        assert request.headers['authorization'] == 'Bearer '+('x'*40)
        values = parse_qs(request.content.decode())
        assert values['resource_types'] == ['[]']
        command, = json.loads(values['commands'][0])
        assert command == dict(type='item_add', uuid=receipt.command_id, temp_id=receipt.temp_id, args=u.payload(config, receipt))
        assert set(command['args']) == {'content', 'description', 'project_id'}
        return httpx.Response(200, json={'sync_status': {receipt.command_id: 'ok'}, 'temp_id_mapping': {receipt.temp_id: 'fixture_task'}})
    assert u.send_todoist(config, receipt, httpx.MockTransport(handler)) == 'fixture_task'


@pytest.mark.parametrize('code', [302, 500, 409])
def test_ambiguous_transport_is_unknown(config, code):
    token(config)
    with pytest.raises(n.NotificationError) as exc:
        u.send_todoist(config, prepared_receipt(config), fixture_get('', code))
    assert exc.value.unknown


@pytest.mark.parametrize('code', [400, 401, 403, 404, 422, 429])
def test_rejected_transport_no_automatic_retry(config, code):
    token(config)
    with pytest.raises(n.NotificationError) as exc:
        u.send_todoist(config, prepared_receipt(config), fixture_get('', code))
    assert not exc.value.unknown and not exc.value.retryable


def test_malformed_ack_and_token_permissions(config):
    receipt = prepared_receipt(config)
    token(config)
    for body in [{}, {'sync_status': {receipt.command_id: 'ok'}, 'temp_id_mapping': {}}, {'sync_status': []}, []]:
        with pytest.raises(n.NotificationError) as exc:
            u.send_todoist(config, receipt, fixture_get(body))
        assert exc.value.unknown
    Path(config.upgrade_notes.token_file).chmod(0o644)
    with pytest.raises(n.NotificationError, match='invalid_todoist_token'):
        u.send_todoist(config, receipt, httpx.MockTransport(Mock(side_effect=AssertionError())))


def test_cli_record_and_disabled_preview(tmp_path, capsys):
    conf = tmp_path/'config'
    conf.write_text(n.Settings(state_dir=str(tmp_path/'state')).model_dump_json())
    conf.chmod(0o600)
    assert n.main(['upgrade-record', '--config', str(conf), '--app-id', 'codex_cli', '--before-version', '1.2.2', '--installed-version', '1.2.3']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'disabled'
    assert not (tmp_path/'state').exists()


def test_watch_once_upgrade_only_does_not_fetch_attention(config, tmp_path, monkeypatch, capsys):
    record(config)
    conf = tmp_path/'config'
    conf.write_text(config.model_dump_json())
    conf.chmod(0o600)
    monkeypatch.setattr(n, 'fetch_snapshot', Mock(side_effect=AssertionError()))
    monkeypatch.setattr(u, 'lookup_notes', lookup)
    monkeypatch.setattr(u, 'send_todoist', sender)
    assert n.main(['once', '--config', str(conf)]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'delivered'


def test_optional_upgrade_failure_does_not_suppress_attention(config, tmp_path, monkeypatch, capsys):
    config.enabled = True
    conf = tmp_path/'config'
    conf.write_text(config.model_dump_json())
    conf.chmod(0o600)
    monkeypatch.setattr(u, 'poll', Mock(side_effect=n.NotificationError('invalid_upgrade_state')))
    monkeypatch.setattr(n, 'fetch_snapshot', Mock(return_value={'fixture': True}))
    attention = Mock(return_value={'status': 'idle'})
    monkeypatch.setattr(n, 'poll', attention)
    assert n.main(['once', '--config', str(conf)]) == 2
    attention.assert_called_once()
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'idle'
    assert result['upgrade_notes'] == {'status': 'error', 'error': 'invalid_upgrade_state'}


def test_corrupt_state_duplicate_commands_and_receipt_limit(config, monkeypatch):
    key = record(config)
    record(config, after='1.2.4')
    p = Path(config.state_dir, 'upgrades.json')
    data = json.loads(p.read_text())
    keys = list(data['entries'])
    data['entries'][keys[1]]['command_id'] = data['entries'][key]['command_id']
    p.write_text(json.dumps(data))
    p.chmod(0o600)
    with pytest.raises(n.NotificationError, match='invalid_upgrade_state'):
        u.poll(config, sender=Mock(side_effect=AssertionError()))


def test_no_token_file_configured_never_reads_other_credentials(config, monkeypatch):
    record(config)
    config.upgrade_notes.token_file = None
    reader = Mock(side_effect=AssertionError())
    monkeypatch.setattr(n, 'protected_read', reader)
    with pytest.raises(n.NotificationError, match='todoist_token_not_configured'):
        u.send_todoist(config, prepared_receipt(config))
    reader.assert_not_called()


def test_notes_and_http_errors_are_sanitized_and_bounded(config):
    def disconnect(request):
        raise httpx.ReadTimeout('PRIVATE upstream response', request=request)
    assert u.lookup_notes(event(), httpx.MockTransport(disconnect)).status == 'lookup_failed'
    for bad in [u.Notes(status='lookup_failed'), u.Notes(status='not_found')]:
        text = u.payload(config, u.UpgradeReceipt(event=event(), notes=bad))['description']
        assert 'published' not in text
        assert 'https://github.com/openai/codex/releases/tag/rust-v1.2.3' in text
    with pytest.raises(ValueError):
        u.Notes(status='not_found', highlights=['Unverified'])
    with pytest.raises(ValueError):
        u.Notes(status='matched', highlights=['x'*251])


def test_atomic_inbox_publish_during_delivery_lock_and_first_bytes(config):
    folder = Path(config.state_dir)
    with n.locked_state(folder):
        key = u.record(config, event())['event_id']
        path = folder/'upgrade-inbox'/f'{key}.json'
        assert path.stat().st_mode & 0o777 == 0o600
        first = path.read_bytes()
        assert u.record(config, event(before='1.0.0'))['status'] == 'already_recorded'
        assert path.read_bytes() == first
    assert u.status(config)['inbox_pending'] == 1
    assert u.poll(config, lookup=lookup, sender=sender)['status'] == 'delivered'
    assert not path.exists()
    assert u.read_state(config).entries[key].event.before_version == '1.2.2'
    assert u.record(config, event())['status'] == 'already_recorded'
    assert not path.exists()


def test_import_crash_before_persistence_keeps_inbox(config, monkeypatch):
    key = u.record(config, event())['event_id']
    monkeypatch.setattr(u, 'save', Mock(side_effect=OSError('synthetic fsync error')))
    with pytest.raises(OSError):
        u.poll(config, sender=Mock(side_effect=AssertionError()))
    assert Path(config.state_dir, 'upgrade-inbox', f'{key}.json').exists()
    assert not u.read_state(config).entries


def test_duplicate_inbox_after_ack_cleanup_crash_never_resends(config):
    record(config)
    u.poll(config, lookup=lookup, sender=sender)
    key = u.event_key(config, event())
    path = Path(config.state_dir, 'upgrade-inbox', f'{key}.json')
    path.write_text(u.UpgradeEnvelope(scope=u.binding(config), event=event()).model_dump_json())
    path.chmod(0o600)
    assert u.poll(config, sender=Mock(side_effect=AssertionError()))['status'] == 'idle'
    assert not path.exists()


def test_explicit_retry_only_known_failure_preserves_identity(config):
    key = record(config)
    def rejected(c, receipt):
        raise n.NotificationError('rejected')
    assert u.poll(config, lookup=lookup, sender=rejected)['status'] == 'blocked'
    assert u.status(config)['status'] == 'blocked'
    first = u.read_state(config).entries[key]
    with pytest.raises(n.NotificationError):
        u.retry_known_failure(config, key, 'wrong')
    assert u.retry_known_failure(config, key, first.command_id)['status'] == 'retry_pending'
    second = u.read_state(config).entries[key]
    assert first.command_id == second.command_id and first.temp_id == second.temp_id and first.notes == second.notes
    def uncertain(c, r):
        raise n.NotificationError('unknown', unknown=True)
    u.poll(config, sender=uncertain)
    with pytest.raises(n.NotificationError, match='upgrade_retry_mismatch'):
        u.retry_known_failure(config, key, first.command_id)


def test_symlink_inbox_or_input_rejected(config, tmp_path):
    folder = Path(config.state_dir)
    n.private_state_directory(folder)
    target = tmp_path/'other'
    target.mkdir(mode=0o700)
    (folder/'upgrade-inbox').symlink_to(target, target_is_directory=True)
    with pytest.raises(n.NotificationError, match='unsafe_notification_state_directory'):
        u.record(config, event())
    (folder/'upgrade-inbox').unlink()
    u.record(config, event())
    key = u.event_key(config, event())
    path = folder/'upgrade-inbox'/f'{key}.json'
    real = tmp_path/'input'
    path.rename(real)
    path.symlink_to(real)
    with pytest.raises(n.NotificationError, match='invalid_upgrade_inbox'):
        u.poll(config, sender=Mock(side_effect=AssertionError()))


def test_renderer_change_cannot_change_safe_retry_wire_args(config, monkeypatch):
    token(config)
    record(config)
    commands = []
    def handler(request):
        command, = json.loads(parse_qs(request.content.decode())['commands'][0])
        commands.append(command)
        if len(commands) == 1:
            raise httpx.ConnectError('synthetic not submitted', request=request)
        return httpx.Response(200, json={'sync_status': {command['uuid']: 'ok'}, 'temp_id_mapping': {command['temp_id']: 'fixture_task'}})
    transport = httpx.MockTransport(handler)
    send = lambda c, r: u.send_todoist(c, r, transport)
    assert u.poll(config, lookup=lookup, sender=send, current=NOW)['status'] == 'retry_pending'
    monkeypatch.setattr(u, 'payload', Mock(side_effect=AssertionError('renderer must not run again')))
    assert u.poll(config, sender=send, current=NOW+5)['status'] == 'delivered'
    assert commands[0] == commands[1]


def test_atomic_inbox_duplicate_import_race_acknowledged(config, monkeypatch):
    key = u.record(config, event())['event_id']
    read = n.protected_read
    def racing_read(path):
        if path.name == f'{key}.json':
            with n.locked_state(Path(config.state_dir)):
                state = u.read_state(config)
                # Avoid invoking racing_read recursively during import.
                monkeypatch.setattr(n, 'protected_read', read)
                u.import_inbox(config, state)
            raise FileNotFoundError()
        return read(path)
    monkeypatch.setattr(n, 'protected_read', racing_read)
    assert u.record(config, event())['status'] == 'already_recorded'
    assert key in u.read_state(config).entries
