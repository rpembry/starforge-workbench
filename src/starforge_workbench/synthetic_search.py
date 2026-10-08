"""Offline query review fixture: no engine, sockets, URLs, notifications or login.

Queries are outbound releases. The existing exact-byte release broker owns consent
and durable send intent; the existing supervisor owns fencing and cancellation.
Only a trusted local composition can review/decide/dispatch/read results. The
model-facing seam can propose queries and receive content-free status only.
Same-user Python objects/private files are not a production security boundary.
"""
from contextlib import contextmanager
import json
from pathlib import Path

from .diagnostic_release import (FakeReleaseEndpoint, ReleaseBroker, ReleaseError,
                                 ReleasePolicy, SEARCH_DESTINATION, STATUSES, _read)
from .diagnostic_vm_bridge import SupervisorDiagnosticFence
from .docker_worker import atomic, private_directory
from .synthetic_review_authority import SyntheticHumanAuthority

MAX_QUERY_BYTES = 1024
MAX_RESULT_BYTES = 4096
MAX_REVISIONS = 8
MAX_AUDIT_EVENTS = 32
MAX_AUDIT_BYTES = 65536
SEARCH_STATUSES = frozenset({'disabled', 'pending', 'declined', 'ready', 'failed',
                            'cancelled', 'uncertain'})


class SearchError(RuntimeError):
    def __init__(self):
        super().__init__('Synthetic search denied')


def search_status(status):
    if type(status) is not str or status not in SEARCH_STATUSES:
        raise SearchError()
    return {'protocol': 'diagnostic.search.status.v1', 'status': status}


class FakeSearchTransport:
    """An allowlisted fake inbox plus local untrusted response bytes, never HTTP.

    The endpoint records the exact approved query once. Results are inert text:
    URLs, redirects and instructions in that text cannot cause another request.
    This is not a pluggable live-engine/network adapter.
    """
    def __init__(self, inbox: Path, *, result=b'Synthetic reference result.'):
        try:
            if type(result) is not bytes or len(result) > MAX_RESULT_BYTES:
                raise SearchError()
            self.endpoint = FakeReleaseEndpoint(inbox, destination=SEARCH_DESTINATION)
            self.result = result
        except Exception:
            raise SearchError() from None

    def local_result(self, binding):
        try:
            if (self.endpoint.reconcile(binding) != 'accepted'
                    or type(self.result) is not bytes or len(self.result) > MAX_RESULT_BYTES):
                raise SearchError()
            return self.result.decode('utf-8', errors='strict')
        except Exception:
            raise SearchError() from None


class SearchApprovalBroker:
    """One serial query per existing attempt, up to eight pre-send revisions.

    No second task queue or cancellation owner. A separate instance/path is
    required for another query; a real composition must bound that allocation.
    This milestone exposes no model-driven allocation or session-wide approval.
    """
    def __init__(self, path: Path, *, authority, transport, fence, clock, enabled=False):
        try:
            if (type(enabled) is not bool or type(authority) is not SyntheticHumanAuthority
                    or authority.destinations != frozenset({SEARCH_DESTINATION})
                    or type(transport) is not FakeSearchTransport
                    or type(transport.endpoint) is not FakeReleaseEndpoint
                    or transport.endpoint.destination != SEARCH_DESTINATION
                    or type(fence) is not SupervisorDiagnosticFence or not callable(clock)):
                raise SearchError()
            self.path = private_directory(path)
            for other in (authority.path, transport.endpoint.path):
                if self.path.is_relative_to(other) or other.is_relative_to(self.path):
                    raise SearchError()
            self.enabled, self.transport, self.fence = enabled, transport, fence
            self.broker = ReleaseBroker(self.path/'query', job_id=fence.binding['job_id'],
                attempt_id=fence.binding['attempt_id'], authenticate=authority,
                endpoint=transport.endpoint, policy=ReleasePolicy(SEARCH_DESTINATION, STATUSES),
                ownership=fence.ownership, clock=clock)
        except Exception:
            raise SearchError() from None

    @contextmanager
    def _scope(self):
        try:
            if not self.enabled:
                raise SearchError()
            with self.fence.scope():
                marker = self.path/'cancel.json'
                if marker.exists() or marker.is_symlink():
                    _read(marker)
                    raise SearchError()
                yield
        except Exception:
            raise SearchError() from None

    def _audit(self, event, snapshot):
        """Private bounded raw evidence only; never a telemetry/status payload."""
        path = self.path/'audit.json'
        audit = _read(path) if path.exists() else {'version': 1, 'events': []}
        if (audit.keys() != {'version', 'events'} or audit['version'] != 1
                or type(audit['events']) is not list or len(audit['events']) >= MAX_AUDIT_EVENTS):
            raise SearchError()
        audit['events'].append({'event': event, 'binding': snapshot.binding()})
        if len((json.dumps(audit, sort_keys=True)+'\n').encode()) > MAX_AUDIT_BYTES:
            raise SearchError()
        atomic(path, audit)

    def _audited(self, event, snapshot):
        path = self.path/'audit.json'
        if not path.exists():
            return False
        audit = _read(path)
        if (audit.keys() != {'version', 'events'} or audit['version'] != 1
                or type(audit['events']) is not list or len(audit['events']) > MAX_AUDIT_EVENTS):
            raise SearchError()
        return {'event': event, 'binding': snapshot.binding()} in audit['events']

    def propose(self, query):
        """Model seam: hold exact UTF-8 locally; never send or return query text."""
        if not self.enabled:
            return search_status('disabled')
        try:
            if type(query) is not str or not 0 < len(query) <= MAX_QUERY_BYTES:
                raise SearchError()
            payload = query.encode('utf-8', errors='strict')
            if not 0 < len(payload) <= MAX_QUERY_BYTES or not query.strip():
                raise SearchError()
            with self._scope():
                if (self.broker.path/'release.json').exists():
                    old = self.broker.review()
                    if self.broker._load()['phase'] in {'review', 'deferred', 'approved'} and old.payload == payload:
                        if not self._audited('presented', old):
                            self._audit('presented', old)  # recover a crash before local audit
                        return search_status('pending')  # idempotent proposal, no new approval
                    if old.revision >= MAX_REVISIONS:
                        raise SearchError()
                snapshot = self.broker.prepare(payload, SEARCH_DESTINATION)
                self._audit('presented', snapshot)
                return search_status('pending')
        except Exception:
            return search_status('failed')

    def review(self):
        """Trusted local human fixture only; exact bytes/destination/revision."""
        with self._scope():
            return self.broker.review()

    def decide(self, snapshot, credential, *, decision='approve', ttl=300):
        """Reuse the human-only authority fixture, never a model bool/string."""
        with self._scope():
            if not self._audited('presented', snapshot):
                raise SearchError()
            # Preflight capacity before issuing any durable approval. The audit
            # event records the proposal, not a claim that authentication succeeded.
            self._audit('decision_requested', snapshot)
            self.broker.decide(snapshot, credential, decision=decision, ttl=ttl)
            try:
                self._audit({'approve': 'approved', 'reject': 'rejected', 'defer': 'deferred'}[decision], snapshot)
            except Exception:
                self.broker.revoke()  # no unrecorded consent may remain usable
                raise SearchError() from None
            return search_status('declined' if decision == 'reject' else 'pending')

    def revoke(self):
        with self._scope():
            snapshot = self.broker.review()
            self.broker.revoke()
            self._audit('revoked', snapshot)
            return search_status('pending')

    def dispatch(self):
        """Trusted separate broker only: exact approved fake send, never retry."""
        try:
            with self._scope():
                snapshot = self.broker.review()
                if not self._audited('approved', snapshot):
                    raise SearchError()  # includes crash after consent, before audit commit
                self._audit('dispatch_requested', snapshot)
                result = self.broker.dispatch()
                return search_status('ready' if result['status'] == 'report_ready' else 'uncertain')
        except Exception:
            return search_status('failed')

    def local_result(self):
        """Local context owner only, never registered as an MCP/cloud response."""
        with self._scope():
            state = self.broker._load()
            if state['phase'] == 'rejected':
                return search_status('declined')
            if state['phase'] != 'released':
                return search_status('pending')
            text = self.transport.local_result(self.broker.review().binding())
            return {'protocol': 'diagnostic.search.result.v1', 'untrusted': True, 'text': text}

    def cancel(self):
        """Terminal local cancellation; already accepted bytes cannot be recalled."""
        try:
            with self._scope():
                atomic(self.path/'cancel.json', {'version': 1, 'cancelled': True})
                # The local marker also blocks result consumption after a send.
                state = self.broker._load() if (self.broker.path/'release.json').exists() else None
                if not state or state['phase'] != 'released':
                    self.broker.cancel()
                if state:
                    self._audit('cancelled', self.broker.review())
                return search_status('cancelled')
        except Exception:
            return search_status('failed')


class SyntheticSearchTool:
    """Proposal-only facade; no decision, dispatch, authority or result method.

    Private object references are fixture plumbing, not OS capability isolation.
    Neither startup CLI nor the installed local Qwen adapter constructs this tool.
    """
    def __init__(self, broker=None):
        if broker is not None and type(broker) is not SearchApprovalBroker:
            raise SearchError()
        self._broker = broker

    def propose(self, query):
        return search_status('disabled') if self._broker is None else self._broker.propose(query)
