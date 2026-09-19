"""Synthetic exact-session delivery tests; no existing OpenCode state is used."""
import json
from pathlib import Path

import pytest

from starforge_workbench.opencode_delivery import DeliveryError, OpenCodeDelivery


SESSION = 'ses_synthetic_session_001'
OTHER = 'ses_synthetic_session_002'
INSTRUCTION = 'instruction_synthetic_001'


class FakeOpenCode:
    def __init__(self, post_result=204, lookup_result=404):
        self.calls = []
        self.post_result = post_result
        self.lookup_result = lookup_result
        self.messages = []

    def __call__(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if method == 'GET' and path == f'/session/{SESSION}':
            return 200, json.dumps({'id': SESSION}).encode()
        if method == 'GET' and path == f'/session/{OTHER}':
            return 404, b''
        if method == 'GET' and path == f'/session/{SESSION}/message?limit=100':
            return 200, json.dumps(self.messages).encode()
        if method == 'GET' and '/message/' in path:
            return self.lookup_result, b''
        if method == 'POST' and path == f'/session/{SESSION}/prompt_async':
            if isinstance(self.post_result, BaseException):
                raise self.post_result
            return self.post_result, b''
        raise AssertionError('Unexpected provider request')


def adapter(tmp_path, fake):
    root = tmp_path / 'private-state'
    root.mkdir(mode=0o700, exist_ok=True)
    return OpenCodeDelivery('http://127.0.0.1:4098', root, 'openai', 'synthetic-model',
                            transport=fake)


def test_exact_session_and_typed_text_only(tmp_path):
    fake = FakeOpenCode()
    delivery = adapter(tmp_path, fake)
    text = '  Synthetic instruction: $(touch /tmp/never-run)\nline two  '
    result = delivery.deliver(INSTRUCTION, SESSION, text)
    assert result.state == 'received' and result.reason == 'api_admitted'
    post = [call for call in fake.calls if call[0] == 'POST']
    assert len(post) == 1
    assert post[0][1] == f'/session/{SESSION}/prompt_async'
    assert post[0][2] == {'messageID': result.message_id,
                          'model': {'providerID': 'openai', 'modelID': 'synthetic-model'},
                          'parts': [{'type': 'text', 'text': text.strip()}]}
    assert not any(OTHER in path for _, path, _ in fake.calls)
    receipt = next((tmp_path / 'private-state').glob('*.json'))
    assert receipt.stat().st_mode & 0o077 == 0
    assert text.strip() not in receipt.read_text()
    assert '/tmp/never-run' not in receipt.read_text()
    assert delivery.deliver(INSTRUCTION, SESSION, text).reason == 'prior_admission'
    assert len([call for call in fake.calls if call[0] == 'POST']) == 1


@pytest.mark.parametrize('text', [
    'first line\nsecond line',
    'Unicode: cafe\u0301, \u4f60\u597d, \U0001f680',
    'shell-looking data: $(touch /tmp/never) `id` ; | && $HOME',
    'tabs\tare\tplain text',
])
def test_newlines_unicode_and_shell_metacharacters_remain_typed_text(tmp_path, text):
    fake = FakeOpenCode()
    result = adapter(tmp_path, fake).deliver(INSTRUCTION, SESSION, text)
    post = [call for call in fake.calls if call[0] == 'POST']
    assert result.state == 'received'
    assert len(post) == 1
    assert post[0][2]['parts'] == [{'type': 'text', 'text': text}]


def test_timeout_and_restart_do_not_resubmit_even_after_404(tmp_path):
    fake = FakeOpenCode(post_result=TimeoutError('synthetic timeout'))
    first = adapter(tmp_path, fake).deliver(INSTRUCTION, SESSION, 'synthetic text')
    assert first.state == 'uncertain'
    second = adapter(tmp_path, fake).deliver(INSTRUCTION, SESSION, 'synthetic text')
    assert second.state == 'uncertain' and second.reason == 'prior_attempt_not_proven'
    assert len([call for call in fake.calls if call[0] == 'POST']) == 1
    fake.lookup_result = 200
    third = adapter(tmp_path, fake).deliver(INSTRUCTION, SESSION, 'synthetic text')
    assert third.state == 'received' and third.reason == 'message_record_found'
    assert len([call for call in fake.calls if call[0] == 'POST']) == 1


def test_controller_interruption_leaves_one_attempt_marker(tmp_path):
    fake = FakeOpenCode(post_result=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        adapter(tmp_path, fake).deliver(INSTRUCTION, SESSION, 'synthetic text')
    result = adapter(tmp_path, fake).deliver(INSTRUCTION, SESSION, 'synthetic text')
    assert result.state == 'uncertain'
    assert len([call for call in fake.calls if call[0] == 'POST']) == 1


def test_existing_message_identity_blocks_new_submission(tmp_path):
    fake = FakeOpenCode(lookup_result=200)
    result = adapter(tmp_path, fake).deliver(INSTRUCTION, SESSION, 'synthetic text')
    assert result.state == 'uncertain' and result.reason == 'message_identity_in_use'
    assert not any(call[0] == 'POST' for call in fake.calls)


def test_unexpected_post_status_is_uncertain_and_not_retried(tmp_path):
    fake = FakeOpenCode(post_result=503)
    delivery = adapter(tmp_path, fake)
    assert delivery.deliver(INSTRUCTION, SESSION, 'synthetic text').state == 'uncertain'
    assert delivery.deliver(INSTRUCTION, SESSION, 'synthetic text').state == 'uncertain'
    assert len([call for call in fake.calls if call[0] == 'POST']) == 1


def test_missing_or_unavailable_session_never_transmits(tmp_path):
    fake = FakeOpenCode()
    delivery = adapter(tmp_path, fake)
    assert delivery.deliver(INSTRUCTION, OTHER, 'synthetic text').state == 'vanished'
    assert not list((tmp_path / 'private-state').glob('*.json'))
    assert not any(call[0] == 'POST' for call in fake.calls)

    def unavailable(method, path, payload=None):
        raise ConnectionRefusedError('synthetic unavailable')
    delivery.transport = unavailable
    result = delivery.deliver(INSTRUCTION, SESSION, 'synthetic text')
    assert result.state == 'unavailable'
    assert not list((tmp_path / 'private-state').glob('*.json'))


def test_busy_admission_and_changed_identity_fail_closed(tmp_path):
    fake = FakeOpenCode(post_result=204)
    delivery = adapter(tmp_path, fake)
    assert delivery.deliver(INSTRUCTION, SESSION, 'synthetic busy input').state == 'received'
    with pytest.raises(DeliveryError, match='changed target'):
        delivery.deliver(INSTRUCTION, OTHER, 'synthetic busy input')
    with pytest.raises(DeliveryError, match='changed target'):
        delivery.deliver(INSTRUCTION, SESSION, 'different text')
    assert len([call for call in fake.calls if call[0] == 'POST']) == 1


def test_correlated_final_response_is_content_free_and_reported_once(tmp_path):
    fake = FakeOpenCode()
    delivery = adapter(tmp_path, fake)
    result = delivery.deliver(
        INSTRUCTION, SESSION, 'synthetic text', 'synthetic_lease_token')
    fake.messages = [
        {'info': {'id': 'msg_tool_step', 'role': 'assistant',
                  'parentID': result.message_id, 'finish': 'tool-calls',
                  'time': {'created': 1000, 'completed': 1100}},
         'parts': [{'type': 'text', 'text': 'PRIVATE INTERMEDIATE OUTPUT'}]},
        {'info': {'id': 'msg_other', 'role': 'assistant',
                  'parentID': 'msg_unrelated', 'finish': 'stop',
                  'time': {'created': 1200, 'completed': 1300}},
         'parts': [{'type': 'text', 'text': 'PRIVATE UNRELATED OUTPUT'}]},
        {'info': {'id': 'msg_final', 'role': 'assistant',
                  'parentID': result.message_id, 'finish': 'stop',
                  'time': {'created': 1400, 'completed': 1500}},
         'parts': [{'type': 'text', 'text': 'PRIVATE FINAL OUTPUT'}]},
    ]

    restarted = adapter(tmp_path, fake)
    evidence = restarted.pending_responses()

    assert len(evidence) == 1
    assert evidence[0].instruction_id == INSTRUCTION
    assert evidence[0].outcome == 'responded'
    assert evidence[0].reason == 'provider_response_without_error'
    assert 'PRIVATE' not in repr(evidence)
    restarted.mark_response(INSTRUCTION, 'reported')
    assert adapter(tmp_path, fake).pending_responses() == []
    receipt = next((tmp_path / 'private-state').glob('*.json')).read_text()
    assert 'synthetic_lease_token' not in receipt
    assert 'PRIVATE' not in receipt


def test_tool_continuation_is_pending_and_correlated_error_is_terminal(tmp_path):
    fake = FakeOpenCode()
    delivery = adapter(tmp_path, fake)
    result = delivery.deliver(
        INSTRUCTION, SESSION, 'synthetic text', 'synthetic_lease_token')
    fake.messages = [{'info': {
        'id': 'msg_tool_step', 'role': 'assistant', 'parentID': result.message_id,
        'finish': 'tool-calls', 'time': {'created': 1000, 'completed': 1100}}}]
    assert delivery.pending_responses() == []

    fake.messages.append({'info': {
        'id': 'msg_error', 'role': 'assistant', 'parentID': result.message_id,
        'time': {'created': 1200}, 'error': {'name': 'SyntheticError',
                                            'message': 'PRIVATE ERROR'}}})
    evidence = delivery.pending_responses()
    assert len(evidence) == 1
    assert evidence[0].reason == 'provider_response_error'
    assert 'PRIVATE' not in repr(evidence)


def test_recovered_error_reports_final_disposition_not_earlier_error(tmp_path):
    fake = FakeOpenCode()
    delivery = adapter(tmp_path, fake)
    result = delivery.deliver(
        INSTRUCTION, SESSION, 'synthetic text', 'synthetic_lease_token')
    fake.messages = [
        {'info': {'id': 'msg_transient_error', 'role': 'assistant',
                  'parentID': result.message_id, 'time': {'created': 1000},
                  'error': {'name': 'SyntheticError', 'message': 'PRIVATE TRANSIENT ERROR'}}},
        {'info': {'id': 'msg_recovered', 'role': 'assistant',
                  'parentID': result.message_id, 'finish': 'stop',
                  'time': {'created': 1100, 'completed': 1200}}},
    ]

    restarted = adapter(tmp_path, fake)
    evidence = restarted.pending_responses()

    assert len(evidence) == 1
    assert evidence[0].outcome == 'responded'
    assert evidence[0].reason == 'provider_response_without_error'
    assert 'PRIVATE' not in repr(evidence)


def test_late_error_after_prior_success_reports_final_disposition(tmp_path):
    fake = FakeOpenCode()
    delivery = adapter(tmp_path, fake)
    result = delivery.deliver(
        INSTRUCTION, SESSION, 'synthetic text', 'synthetic_lease_token')
    fake.messages = [
        {'info': {'id': 'msg_first_stop', 'role': 'assistant',
                  'parentID': result.message_id, 'finish': 'stop',
                  'time': {'created': 1000, 'completed': 1100}}},
        {'info': {'id': 'msg_later_error', 'role': 'assistant',
                  'parentID': result.message_id, 'time': {'created': 1200},
                  'error': {'name': 'SyntheticError', 'message': 'PRIVATE LATER ERROR'}}},
    ]

    restarted = adapter(tmp_path, fake)
    evidence = restarted.pending_responses()

    assert len(evidence) == 1
    assert evidence[0].outcome == 'responded'
    assert evidence[0].reason == 'provider_response_error'
    assert 'PRIVATE' not in repr(evidence)


def test_take_preview_returns_text_excerpt_once_then_nothing(tmp_path):
    fake = FakeOpenCode()
    delivery = adapter(tmp_path, fake)
    result = delivery.deliver(
        INSTRUCTION, SESSION, 'synthetic text', 'synthetic_lease_token')
    fake.messages = [{'info': {
        'id': 'msg_final', 'role': 'assistant', 'parentID': result.message_id,
        'finish': 'stop', 'time': {'created': 1000, 'completed': 1100}},
        'parts': [{'type': 'text', 'text': 'Synthetic final answer.'},
                  {'type': 'text', 'text': 'Second part.'}]}]

    restarted = adapter(tmp_path, fake)
    evidence = restarted.pending_responses()

    assert len(evidence) == 1
    assert restarted.take_preview(INSTRUCTION) == 'Synthetic final answer. Second part.'
    assert restarted.take_preview(INSTRUCTION) is None


def test_take_preview_falls_back_to_error_message_and_is_bounded(tmp_path):
    fake = FakeOpenCode()
    delivery = adapter(tmp_path, fake)
    result = delivery.deliver(
        INSTRUCTION, SESSION, 'synthetic text', 'synthetic_lease_token')
    fake.messages = [{'info': {
        'id': 'msg_error', 'role': 'assistant', 'parentID': result.message_id,
        'time': {'created': 1000},
        'error': {'name': 'SyntheticError', 'message': 'x' * 600}}}]

    restarted = adapter(tmp_path, fake)
    restarted.pending_responses()

    excerpt = restarted.take_preview(INSTRUCTION)
    assert excerpt == 'x' * 500


def test_mark_response_clears_preview_even_if_never_taken(tmp_path):
    fake = FakeOpenCode()
    delivery = adapter(tmp_path, fake)
    result = delivery.deliver(
        INSTRUCTION, SESSION, 'synthetic text', 'synthetic_lease_token')
    fake.messages = [{'info': {
        'id': 'msg_final', 'role': 'assistant', 'parentID': result.message_id,
        'finish': 'stop', 'time': {'created': 1000, 'completed': 1100}},
        'parts': [{'type': 'text', 'text': 'Never fetched.'}]}]

    restarted = adapter(tmp_path, fake)
    restarted.pending_responses()
    restarted.mark_response(INSTRUCTION, 'reported')

    assert restarted.take_preview(INSTRUCTION) is None


@pytest.mark.parametrize('origin', [
    'http://example.com:4098', 'http://0.0.0.0:4098', 'https://127.0.0.1:4098',
    'http://127.0.0.1:4098/path', 'http://127.0.0.1:4098?x=1',
    'http://user:pass@127.0.0.1:4098', 'http://127.0.0.1:0',
])
def test_refuses_nonloopback_or_credentialed_origin(tmp_path, origin):
    root = tmp_path / 'private-state'
    root.mkdir(mode=0o700)
    with pytest.raises(DeliveryError):
        OpenCodeDelivery(origin, root, 'openai', 'synthetic-model', transport=FakeOpenCode())


@pytest.mark.parametrize('character', ['\x00', '\x01', '\x0b', '\x1f', '\x7f', '\x80', '\x85', '\x9f'])
def test_rejects_unsafe_local_state_and_control_characters(tmp_path, character):
    root = tmp_path / 'public-state'
    root.mkdir(mode=0o755)
    with pytest.raises(DeliveryError):
        OpenCodeDelivery('http://127.0.0.1:4098', root, 'openai', 'synthetic-model')
    delivery = adapter(tmp_path, FakeOpenCode())
    with pytest.raises(DeliveryError):
        delivery.deliver(INSTRUCTION, SESSION, 'bad' + character + 'text')
    with pytest.raises(DeliveryError):
        delivery.deliver(INSTRUCTION, SESSION, '   ')
    with pytest.raises(DeliveryError):
        delivery.deliver(INSTRUCTION, SESSION, 'x' * 2001)
    assert not list(Path(delivery.root).glob('*.json'))
