"""Unit tests for the in-memory, non-persistent response-preview relay."""
from workbench.response_preview import ResponsePreviewHub


def test_publish_with_no_subscribers_is_dropped_not_queued():
    hub = ResponsePreviewHub()
    delivered = hub.publish('instruction_1', 'registered_session_1',
                            'provider_response_without_error', 'hello')
    assert delivered is False


def test_subscriber_receives_published_event():
    hub = ResponsePreviewHub()
    queue = hub.subscribe()
    delivered = hub.publish('instruction_1', 'registered_session_1',
                            'provider_response_without_error', 'hello')
    assert delivered is True
    assert queue.get_nowait() == {
        'instruction_id': 'instruction_1', 'registered_session_id': 'registered_session_1',
        'outcome': 'provider_response_without_error',
        'excerpt': 'hello'}


def test_unsubscribed_queue_no_longer_receives_events():
    hub = ResponsePreviewHub()
    queue = hub.subscribe()
    hub.unsubscribe(queue)
    delivered = hub.publish('instruction_1', 'registered_session_1',
                            'provider_response_without_error', 'hello')
    assert delivered is False
    assert queue.empty()


def test_a_full_slow_subscriber_queue_is_skipped_not_raised():
    hub = ResponsePreviewHub(max_queue=1)
    slow = hub.subscribe()
    slow.put_nowait({'stale': 'entry already queued'})
    fresh = hub.subscribe()
    delivered = hub.publish('instruction_1', 'registered_session_1',
                            'provider_response_without_error', 'hello')
    assert delivered is True  # fresh subscriber still got it
    assert slow.get_nowait() == {'stale': 'entry already queued'}  # untouched, not replaced
    assert fresh.get_nowait()['excerpt'] == 'hello'
