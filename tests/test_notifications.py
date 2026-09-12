import copy
from datetime import datetime, timezone
import json
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs

import httpx
import pytest

from workbench import notifications as n

NOW = 1800000000.0


def snapshot(items, current=NOW):
    return {'generated_at': datetime.fromtimestamp(current, timezone.utc).isoformat(), 'items': items}


def item(identity='fixture', kind='approval_needed', **extra):
    return dict(id=identity, kind=kind, progress='unknown', reason='Review required', title='PRIVATE task fixture',
                evidence=[{'timestamps': {'heartbeat_at': 'earlier'}}], **extra)


@pytest.fixture
def config(tmp_path):
    return n.Settings(enabled=True, dashboard_url='https://dashboard.example.test',
                      api_client_file=str(tmp_path/'client.json'), credentials_file=str(tmp_path/'pushover.env'),
                      state_dir=str(tmp_path/'notifications'), min_interval_seconds=5)


def tick(config, items, sender, current=NOW, **kwargs):
    return n.poll(config, snapshot(items, current), sender=sender, current=current, **kwargs)


def private_write(path, content):
    path.write_text(content)
    path.chmod(0o600)


def credentials(config):
    private_write(Path(config.credentials_file), 'PUSHOVER_USER_KEY='+('u'*30)+'\nPUSHOVER_API_TOKEN='+('a'*30)+'\nPUSHOVER_TITLE="Fixture title"\n')


def test_delivery_dedup_restart_heartbeat_recovery_and_meaningful_change(config):
    sender = Mock()
    a = item()
    assert tick(config, [a], sender)['status'] == 'delivered'
    again = copy.deepcopy(a)
    again['evidence'][0]['timestamps']['heartbeat_at'] = 'new heartbeat'
    assert tick(config, [again], sender, NOW+10)['status'] == 'quiet'
    # Reconstruct settings and read persisted state to model a worker restart.
    restarted = n.Settings.model_validate_json(config.model_dump_json())
    assert tick(restarted, [again], sender, NOW+86400)['status'] == 'quiet'
    changed = {**again, 'progress': 'needs_review'}
    assert tick(config, [changed], sender, NOW+86401)['status'] == 'delivered'
    assert tick(config, [], sender, NOW+86402)['status'] == 'quiet'
    assert tick(config, [changed], sender, NOW+86407)['status'] == 'delivered'
    assert sender.call_count == 3
    state = Path(config.state_dir)/'delivery.json'
    assert state.stat().st_mode & 0o777 == 0o600
    assert state.parent.stat().st_mode & 0o777 == 0o700
    assert 'PRIVATE' not in state.read_text() and 'fixture' not in state.read_text()


def test_categories_coalesce_and_routine_details_do_not_notify(config):
    config.categories = ['collector_health']
    sender = Mock()
    entries = [item('outage', 'collector_health'), item('approval')]
    assert tick(config, entries, sender)['count'] == 1
    payload = sender.call_args.args[1]
    assert payload['url'] == config.dashboard_url
    assert 'collector visibility' in payload['message'] and 'not confirmed task failure' in payload['message']
    assert 'PRIVATE' not in json.dumps(payload)
    entries[0]['title'] = 'Different source label'
    assert tick(config, entries, sender, NOW+10)['status'] == 'quiet'
    entries[0]['reason'] = 'No heartbeat for more than 90 seconds'
    assert tick(config, entries, sender, NOW+11)['status'] == 'delivered'
    assert tick(config, [], sender, NOW+12)['status'] == 'quiet'
    assert tick(config, entries, sender, NOW+20)['status'] == 'delivered'


def test_details_require_opt_in_and_message_is_bounded(config):
    config.include_details = True
    sender = Mock()
    entries = [{**item(str(i)), 'title': 'Sensitive fixture '+('x'*500)} for i in range(40)]
    assert tick(config, entries, sender)['count'] == 40
    assert sender.call_count == 1
    message = sender.call_args.args[1]['message']
    assert 'Sensitive fixture' in message and len(message) <= 1024


def test_disabled_and_preview_never_deliver_read_credentials_or_write_state(config, monkeypatch, tmp_path):
    sender = Mock(side_effect=AssertionError('unexpected delivery'))
    config.enabled = False
    monkeypatch.setattr(n, 'pushover_credentials', Mock(side_effect=AssertionError('credentials accessed')))
    assert n.poll(config, {}, sender=sender) == {'status': 'disabled'}
    result = tick(config, [item()], sender, preview=True)
    assert result['status'] == 'preview' and result['pending'] == 1
    assert not Path(config.state_dir).exists()
    assert n.settings(tmp_path/'missing.json').enabled is False
    config_path = tmp_path/'disabled.json'
    private_write(config_path, config.model_dump_json())
    monkeypatch.setattr(n, 'fetch_snapshot', Mock(side_effect=AssertionError('unexpected API call')))
    assert n.main(['once', '--config', str(config_path)]) == 0
    assert n.main(['test', '--config', str(config_path)]) == 0
    sender.assert_not_called()


def test_failure_pending_survives_restart_and_bounded_backoff(config):
    sender = Mock(side_effect=n.NotificationError('delivery_unavailable', retryable=True))
    assert tick(config, [item()], sender)['status'] == 'retry_pending'
    assert tick(config, [item()], sender, NOW+1)['status'] == 'backoff'
    assert tick(config, [item()], sender, NOW+30)['status'] == 'retry_pending'
    assert tick(config, [item()], sender, NOW+90)['status'] == 'blocked'
    assert tick(config, [item()], sender, NOW+9999)['status'] == 'blocked'
    assert sender.call_count == 3
    stored = n.read_state(Path(config.state_dir), config)
    assert all(not e.delivered for e in stored.entries.values())
    success = Mock()
    assert tick(config, [item()], success, NOW+10000, retry_pending=True)['status'] == 'delivered'
    assert tick(config, [item()], success, NOW+10010)['status'] == 'quiet'
    assert success.call_count == 1


def test_resolved_pending_is_not_sent_and_new_items_respect_global_rate_limit(config):
    config.min_interval_seconds = 300
    sender = Mock()
    tick(config, [item()], sender)
    many = [item(str(i)) for i in range(100)]
    assert tick(config, many, sender, NOW+1)['status'] == 'backoff'
    assert tick(config, [], sender, NOW+2)['status'] == 'quiet'
    assert tick(config, [], sender, NOW+301)['status'] == 'quiet'
    assert sender.call_count == 1
    assert tick(config, many, sender, NOW+302)['count'] == 100
    assert sender.call_count == 2  # One catch-up summary, not 100 messages.


def test_bad_snapshot_and_api_failure_cannot_rearm_delivered_items(config, monkeypatch):
    sender = Mock()
    tick(config, [item()], sender)
    before = (Path(config.state_dir)/'delivery.json').read_bytes()
    for bad in [{}, snapshot([], NOW-1000), snapshot([{}]), snapshot([item(), {**item(), 'reason':'conflict'}])]:
        with pytest.raises(n.NotificationError): n.poll(config, bad, sender, current=NOW+1)
    with pytest.raises(n.NotificationError, match='out_of_order'):
        n.poll(config, snapshot([], NOW-1), sender, current=NOW+1)
    monkeypatch.setattr(n, 'client', Mock(side_effect=httpx.ConnectError('PRIVATE transport detail')))
    with pytest.raises(n.NotificationError, match='attention_api_unavailable'):
        n.fetch_snapshot(config)
    assert (Path(config.state_dir)/'delivery.json').read_bytes() == before
    assert tick(config, [item()], sender, NOW+2)['status'] == 'quiet'
    assert sender.call_count == 1


def test_corrupt_state_permissions_lock_and_source_mismatch_fail_closed(config):
    folder = Path(config.state_dir)
    with n.locked_state(folder):
        with pytest.raises(n.NotificationError, match='already_running'):
            with n.locked_state(folder): pass
    private_write(folder/'delivery.json', 'invalid-json PRIVATE')
    with pytest.raises(n.NotificationError, match='invalid_notification_state'):
        tick(config, [item()], Mock())
    n.save_state(folder, n.DeliveryState(scope=n.scope(config)))
    (folder/'delivery.json').chmod(0o644)
    with pytest.raises(n.NotificationError, match='unsafe_private_file'):
        tick(config, [item()], Mock())
    (folder/'delivery.json').chmod(0o600)
    config.dashboard_url = 'https://other.example.test'
    with pytest.raises(n.NotificationError, match='source_mismatch'):
        tick(config, [item()], Mock())


def test_missing_and_invalid_credentials_keep_pending_without_exposure(config, capsys):
    assert tick(config, [item()], n.send_pushover)['status'] == 'blocked'
    assert not next(iter(n.read_state(Path(config.state_dir), config).entries.values())).delivered
    private_write(Path(config.credentials_file), 'PUSHOVER_USER_KEY=private-invalid-value\n')
    with pytest.raises(n.NotificationError) as exc: n.pushover_credentials(config)
    assert 'private-invalid-value' not in str(exc.value)
    credentials(config)
    Path(config.credentials_file).chmod(0o644)
    with pytest.raises(n.NotificationError): n.pushover_credentials(config)
    assert not capsys.readouterr().out


@pytest.mark.parametrize('status,body,retryable', [(400, {'status':0,'errors':['PRIVATE']}, False),
    (429, {'status':0}, False), (503, {'status':0}, False), (200, {'status':0}, False),
    (200, [], False), (302, {}, False)])
def test_mocked_rejections_never_echo_body_or_credentials(config, status, body, retryable):
    credentials(config)
    transport = httpx.MockTransport(lambda r: httpx.Response(status, json=body))
    with pytest.raises(n.NotificationError) as exc:
        n.send_pushover(config, n.message([item()], config), transport)
    assert exc.value.retryable == retryable
    assert 'PRIVATE' not in str(exc.value) and 'u'*30 not in str(exc.value)


def test_mocked_delivery_success_and_timeout(config):
    credentials(config)
    def accept(request):
        assert str(request.url) == n.ENDPOINT
        fields = parse_qs(request.content.decode())
        assert fields['user'] == ['u'*30] and fields['token'] == ['a'*30]
        assert fields['title'] == ['Fixture title']
        assert fields['url'] == [config.dashboard_url]
        assert 'authorization' not in request.headers and 'cf-access-client-secret' not in request.headers
        return httpx.Response(200, json={'status':1})
    n.send_pushover(config, n.message([item()], config), httpx.MockTransport(accept))
    def timeout(request): raise httpx.ReadTimeout('PRIVATE timeout details', request=request)
    with pytest.raises(n.NotificationError) as exc:
        n.send_pushover(config, n.message([item()], config), httpx.MockTransport(timeout))
    assert exc.value.unknown and not exc.value.retryable and 'PRIVATE' not in str(exc.value)


def test_test_command_sends_only_with_explicit_flag(config, tmp_path, monkeypatch):
    config.enabled = False
    path = tmp_path/'config.json'
    private_write(path, config.model_dump_json())
    send = Mock()
    monkeypatch.setattr(n, 'send_pushover', send)
    assert n.main(['test', '--config', str(path)]) == 0
    send.assert_not_called()
    assert n.main(['test', '--send', '--config', str(path)]) == 0
    send.assert_called_once()
    assert not Path(config.state_dir).exists()


def test_api_consumption_is_read_only_and_rejected_delivery_blocks_until_explicit_retry(config, monkeypatch):
    def respond(request):
        assert request.method == 'GET' and request.url.path == '/api/attention'
        return httpx.Response(200, json=snapshot([item()]))
    monkeypatch.setattr(n, 'client', lambda **kw: httpx.Client(base_url='https://workbench.example.test', transport=httpx.MockTransport(respond)))
    assert n.fetch_snapshot(config) == snapshot([item()])
    rejected = Mock(side_effect=n.NotificationError('delivery_rejected'))
    assert tick(config, [item()], rejected)['status'] == 'blocked'
    assert tick(config, [item()], rejected, NOW+10000)['status'] == 'blocked'
    rejected.assert_called_once()


def test_config_errors_and_symlink_credentials_are_safe(config, tmp_path, capsys):
    path = tmp_path/'bad-config.json'
    private_write(path, '{"enabled": "PRIVATE_VALUE"}')
    assert n.main(['once', '--config', str(path)]) == 2
    output = capsys.readouterr().out
    assert 'PRIVATE_VALUE' not in output and 'invalid_notification_config' in output
    credentials(config)
    link = tmp_path/'credential-link'
    link.symlink_to(config.credentials_file)
    config.credentials_file = str(link)
    with pytest.raises(n.NotificationError): n.pushover_credentials(config)


def test_default_spacing_survives_restart_and_failed_send_never_acknowledges(config):
    config.min_interval_seconds = 300
    credentials(config)
    def failure(settings, payload):
        raise n.NotificationError('delivery_not_submitted', retryable=True)
    assert tick(config, [item()], failure)['status'] == 'retry_pending'
    sender = Mock()
    assert tick(config, [item('new')], sender, NOW+299)['status'] == 'backoff'
    assert not next(iter(n.read_state(Path(config.state_dir), config).entries.values())).delivered
    assert tick(config, [item('new')], sender, NOW+300)['status'] == 'delivered'
    assert sender.call_count == 1


def test_existing_dotenv_spacing_and_unquoted_title_are_supported_without_execution(config):
    private_write(Path(config.credentials_file), '# Fixture only\nexport PUSHOVER_USER_KEY = '+('u'*30)+'\nPUSHOVER_API_TOKEN='+('a'*30)+'\nPUSHOVER_TITLE=Example notification title # comment\n')
    assert n.pushover_credentials(config) == ('u'*30, 'a'*30, 'Example notification title')
    private_write(Path(config.credentials_file), 'PUSHOVER_USER_KEY='+('u'*30)+'\nPUSHOVER_API_TOKEN='+('a'*30)+'\nPUSHOVER_TITLE="$(touch should-not-exist)"\n')
    assert n.pushover_credentials(config)[2] == '$(touch should-not-exist)'


@pytest.mark.parametrize('boundary', ['before_transport', 'after_acceptance', 'before_ack_persistence'])
def test_crash_boundaries_hold_unknown_across_restart(config, monkeypatch, boundary):
    accepted = []
    original = n.save_state
    def save(folder, state):
        if boundary == 'before_ack_persistence' and state.attempt and state.attempt.outcome == 'delivered':
            raise SystemExit('synthetic persistence crash')
        original(folder, state)
    monkeypatch.setattr(n, 'save_state', save)
    def sender(settings, payload):
        if boundary != 'before_transport': accepted.append(payload)
        if boundary != 'before_ack_persistence': raise SystemExit('synthetic crash')
    with pytest.raises(SystemExit): tick(config, [item()], sender)
    monkeypatch.setattr(n, 'save_state', original)
    retry = Mock()
    result = tick(config, [item()], retry, NOW+10000, retry_pending=True)
    assert result['status'] == 'unknown'
    assert result['attempt']['blocks'] == 'whole_worker'
    assert result['attempt']['affected'] == 1
    assert tick(config, [item('unrelated')], retry, NOW+10001)['status'] == 'unknown'
    retry.assert_not_called()
    assert len(accepted) == (0 if boundary == 'before_transport' else 1)
    view = tick(config, [item('unrelated')], retry, NOW+10002, preview=True)
    assert view['attempt']['id'] == result['attempt']['id'] and view['blocked']


@pytest.mark.parametrize('failure', ['read', 'write', 'json', 'server'])
def test_ambiguous_http_outcome_is_persisted_unknown(config, failure):
    credentials(config)
    def transport(request):
        if failure == 'read': raise httpx.ReadTimeout('private', request=request)
        if failure == 'write': raise httpx.WriteError('private', request=request)
        return httpx.Response(200 if failure == 'json' else 503, text='invalid acknowledgement')
    def sender(settings, payload):
        n.send_pushover(settings, payload, httpx.MockTransport(transport))
    assert tick(config, [item()], sender)['status'] == 'unknown'
    unexpected = Mock()
    assert tick(config, [item()], unexpected, NOW+10000)['status'] == 'unknown'
    unexpected.assert_not_called()


def test_connect_failure_is_known_safe_and_fresh_item_has_own_budget(config):
    credentials(config)
    config.max_attempts = 1
    def transport(request): raise httpx.ConnectTimeout('private', request=request)
    def sender(settings, payload): n.send_pushover(settings, payload, httpx.MockTransport(transport))
    assert tick(config, [item()], sender)['status'] == 'blocked'
    state = n.read_state(Path(config.state_dir), config)
    assert state.attempt.outcome == 'known_failed'
    success = Mock()
    assert tick(config, [item(), item('fresh')], success, NOW+100)['count'] == 1
    assert tick(config, [item(), item('fresh')], success, NOW+200)['status'] == 'blocked'
    success.assert_called_once()


def test_resolution_requires_matching_attempt_and_duplicate_acknowledgement(config):
    unknown = Mock(side_effect=n.NotificationError('delivery_unknown', unknown=True))
    attempt = tick(config, [item()], unknown)['attempt']['id']
    with pytest.raises(n.NotificationError, match='mismatch'):
        n.resolve(config, 'wrong-attempt', 'delivered')
    with pytest.raises(n.NotificationError, match='acknowledgement_required'):
        n.resolve(config, attempt, 'retry')
    n.resolve(config, attempt, 'retry', acknowledge_duplicate=True)
    success = Mock()
    assert tick(config, [item()], success, NOW+100)['status'] == 'delivered'
    success.assert_called_once()


def test_resolution_cannot_acknowledge_changed_fingerprint_or_recurrent_incident(config):
    unknown = Mock(side_effect=n.NotificationError('delivery_unknown', unknown=True))
    attempt = tick(config, [item()], unknown)['attempt']['id']
    # Recovery while held must not discard evidence of the unknown attempt.
    assert tick(config, [], unknown, NOW+1)['status'] == 'unknown'
    assert tick(config, [item()], unknown, NOW+2)['status'] == 'unknown'
    n.resolve(config, attempt, 'delivered')
    success = Mock()
    assert tick(config, [item()], success, NOW+100)['status'] == 'delivered'
    attempt = tick(config, [item('other')], unknown, NOW+200)['attempt']['id']
    changed = {**item('other'), 'reason': 'Changed reason'}
    tick(config, [changed], unknown, NOW+201)
    n.resolve(config, attempt, 'delivered')
    assert tick(config, [changed], success, NOW+300)['status'] == 'delivered'


def test_confirmed_resolution_suppresses_only_exact_members_and_can_run_disabled(config, tmp_path, capsys):
    unknown = Mock(side_effect=n.NotificationError('delivery_unknown', unknown=True))
    attempt = tick(config, [item()], unknown)['attempt']['id']
    config.enabled = False
    path = tmp_path/'config.json'
    private_write(path, config.model_dump_json())
    assert n.main(['status', '--config', str(path)]) == 0
    assert 'whole_worker' in capsys.readouterr().out
    assert n.main(['resolve', '--attempt-id', attempt, '--outcome', 'delivered', '--config', str(path)]) == 0
    config.enabled = True
    success = Mock()
    assert tick(config, [item()], success, NOW+100)['status'] == 'quiet'
    success.assert_not_called()


@pytest.mark.parametrize('attempted', [0, 1, 3])
def test_version_one_migration_preserves_acknowledgements_and_holds_uncertain_attempts(config, attempted):
    folder = Path(config.state_dir)
    folder.mkdir(mode=0o700)
    old = n.LegacyState(scope=n.scope(config), attempts=attempted,
        entries={n.digest('done'): n.LegacyEntry(fingerprint=n.fingerprint(item('done'), False), delivered=True),
                 n.digest('pending'): n.LegacyEntry(fingerprint=n.fingerprint(item('pending'), False))})
    private_write(folder/'delivery.json', old.model_dump_json())
    before = (folder/'delivery.json').read_bytes()
    preview = tick(config, [item('done'), item('pending')], Mock(), preview=True)
    assert preview['blocked'] == bool(attempted)
    assert (folder/'delivery.json').read_bytes() == before
    sender = Mock()
    result = tick(config, [item('done'), item('pending')], sender)
    assert result['status'] == ('unknown' if attempted else 'delivered')
    assert sender.call_count == (0 if attempted else 1)
    assert n.read_state(folder, config).version == 2


def test_v1_preview_attempt_id_is_stable_and_resolvable_while_disabled(config):
    folder = Path(config.state_dir)
    folder.mkdir(mode=0o700)
    old = n.LegacyState(scope=n.scope(config), attempts=1,
        entries={n.digest('fixture'): n.LegacyEntry(fingerprint=n.fingerprint(item(), False))})
    private_write(folder/'delivery.json', old.model_dump_json())
    first = tick(config, [item()], Mock(), preview=True)['attempt']['id']
    assert tick(config, [item()], Mock(), preview=True)['attempt']['id'] == first
    config.enabled = False
    n.resolve(config, first, 'delivered')
    assert n.read_state(folder, config).version == 2
    config.enabled = True
    sender = Mock()
    assert tick(config, [item()], sender)['status'] == 'quiet'
    sender.assert_not_called()
