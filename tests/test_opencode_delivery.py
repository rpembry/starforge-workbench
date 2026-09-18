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

    def __call__(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if method == 'GET' and path == f'/session/{SESSION}':
            return 200, json.dumps({'id': SESSION}).encode()
        if method == 'GET' and path == f'/session/{OTHER}':
            return 404, b''
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


def test_rejects_unsafe_local_state_and_control_characters(tmp_path):
    root = tmp_path / 'public-state'
    root.mkdir(mode=0o755)
    with pytest.raises(DeliveryError):
        OpenCodeDelivery('http://127.0.0.1:4098', root, 'openai', 'synthetic-model')
    delivery = adapter(tmp_path, FakeOpenCode())
    with pytest.raises(DeliveryError):
        delivery.deliver(INSTRUCTION, SESSION, 'bad\x00text')
    with pytest.raises(DeliveryError):
        delivery.deliver(INSTRUCTION, SESSION, 'bad\x7ftext')
    with pytest.raises(DeliveryError):
        delivery.deliver(INSTRUCTION, SESSION, 'bad\x85text')
    with pytest.raises(DeliveryError):
        delivery.deliver(INSTRUCTION, SESSION, '   ')
    assert not list(Path(delivery.root).glob('*.json'))
