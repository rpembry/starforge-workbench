"""One synthetic batch through the independently owned outbound boundary."""
import hashlib
import hmac
import json

import pytest

from starforge_workbench.diagnostic_batch import DiagnosticWorker, MockQwenAdapter
from starforge_workbench.diagnostic_release import (
    Approval, FakeReleaseEndpoint, ReleaseBroker, ReleaseError, ReleasePolicy, STATUSES,
)


class LocalTestOperator:
    """Test-only local authentication: never made available to the fake model."""
    def __init__(self):
        self.reviews = 0
        self.revoked = set()

    def _receipt(self, binding):
        return hmac.new(b'synthetic-test-only-key',
                        json.dumps(binding, sort_keys=True).encode(), hashlib.sha256).hexdigest()

    def authenticate(self, credential, binding):
        if credential != 'synthetic-human-session':
            raise ValueError('Unauthenticated')
        self.reviews += 1
        return Approval('synthetic-operator', self._receipt(binding))

    def verify(self, approval, binding):
        return (approval.principal == 'synthetic-operator'
                and approval.receipt not in self.revoked
                and hmac.compare_digest(approval.receipt, self._receipt(binding)))

    def revoke(self, approval, binding):
        self.revoked.add(approval.receipt)


def test_one_batch_exact_review_and_release(tmp_path):
    attempt = tmp_path/'attempt'
    inbox = tmp_path/'inbox'
    attempt.mkdir(mode=0o700)
    inbox.mkdir(mode=0o700)
    clock = lambda: 1000
    owner = ['active']
    worker = DiagnosticWorker(attempt, 'job-1', 'attempt-1', ownership=lambda: owner[0], clock=clock)
    request = {'protocol': 'diagnostic.batch.v1', 'job_id': 'job-1',
               'attempt_id': 'attempt-1', 'operation_id': 'operation-1',
               'diagnostic_type': 'capacity.v1', 'target_id': 'synthetic-1',
               'parameters': {}, 'deadline': 1060,
               'budget': {'steps': 1, 'max_output_bytes': 4096}}
    operator = LocalTestOperator()
    endpoint = FakeReleaseEndpoint(inbox)
    broker = ReleaseBroker(attempt, job_id='job-1', attempt_id='attempt-1',
                           authenticate=operator, endpoint=endpoint,
                           ownership=lambda: owner[0],
                           policy=ReleasePolicy('coordinator.synthetic', STATUSES), clock=clock)

    local_state = worker.run(request, MockQwenAdapter())
    cloud_context = [broker.status(local_state['status'])]
    report = worker.local_report()
    assert json.loads(report)['summary'] == 'Synthetic configured workers exceed capacity by 2.'
    assert b'SYNTHETIC_SOURCE_CANARY' in report
    assert b'SYNTHETIC_LOG_CANARY' in report
    assert b'SYNTHETIC_SCREENSHOT_CANARY' in report
    assert 'CANARY' not in json.dumps(cloud_context)
    assert not (inbox/'inbox.json').exists()

    # One concise operator-authored outbound payload, not automatic raw report export.
    payload = b'Synthetic capacity deficit: 2 units. Local configuration review needed.'
    review = broker.prepare(payload)
    assert review == broker.review()
    with pytest.raises(ReleaseError):
        broker.dispatch()
    with pytest.raises(ReleaseError):
        broker.decide(review, 'model-claimed-operator')
    assert not (inbox/'inbox.json').exists()
    broker.decide(review, 'synthetic-human-session')
    broker.dispatch()
    assert operator.reviews == 1
    ledger = json.loads((inbox/'inbox.json').read_text())
    assert len(ledger) == 1
    assert ledger[review.operation_id] == review.binding()
    assert 'CANARY' not in json.dumps(ledger)
    assert worker.run(request, MockQwenAdapter()) == local_state
    with pytest.raises(ReleaseError):
        broker.dispatch()  # delivery does not permit replay
