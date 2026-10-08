"""Exact-context handoff over the existing registered instruction queue."""
from pathlib import Path
import re
import httpx

from workbench.collector import _registration_state

OPAQUE_ID = re.compile(r'^[A-Za-z0-9_-]{16,128}$')
KEY = re.compile(r'^[A-Za-z0-9._~-]{16,128}$')


class Handoff:
    def __init__(self, contexts, registration_state, request, verify_generation):
        self.contexts = contexts
        self.registration_state = Path(registration_state) if registration_state else None
        self.request = request
        self.verify_generation = verify_generation

    def target(self, context_id):
        matches = [item for item in self.contexts if item.get('id') == context_id and item.get('enabled')]
        if len(matches) != 1:
            return {'outcome': 'copyable', 'reason': 'missing_or_ambiguous_context'}
        context = matches[0]
        if context.get('provider') != 'opencode':
            return {'outcome': 'copyable', 'reason': 'provider_delivery_unsupported',
                    'context_id': context_id}
        if self.registration_state is None or not self.registration_state.is_file():
            return {'outcome': 'copyable', 'reason': 'registration_unavailable',
                    'context_id': context_id}
        try:
            state = _registration_state(self.registration_state)
            item = state['contexts'].get(context_id)
            if not item or item['provider'] != 'opencode' or not OPAQUE_ID.fullmatch(item['id']):
                raise ValueError('registration mismatch')
            session_id = item['id']
            if not self.verify_generation(session_id):
                raise ValueError('generation changed')
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            return {'outcome': 'copyable', 'reason': 'target_unverified',
                    'context_id': context_id}
        try:
            row = self.request('GET', '/api/registered-sessions/'+session_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {401, 403}:
                return {'outcome': 'denied', 'reason': 'operator_api_denied'}
            return {'outcome': 'uncertain', 'reason': 'operator_api_unavailable'}
        except Exception:
            return {'outcome': 'uncertain', 'reason': 'operator_api_unavailable'}
        if not isinstance(row, dict):
            return {'outcome': 'uncertain', 'reason': 'operator_api_unavailable'}
        if (row.get('id') != session_id or row.get('provider') != 'opencode' or
                row.get('visibility') != 'fresh' or row.get('evidence_state') in {'unknown', 'stopped'}):
            return {'outcome': 'copyable', 'reason': 'session_unavailable',
                    'context_id': context_id}
        return {'outcome': 'ready', 'context_id': context_id, 'session_id': session_id}

    def send(self, context_id, expected_session_id, text, idempotency_key):
        if not isinstance(expected_session_id, str) or not OPAQUE_ID.fullmatch(expected_session_id):
            raise ValueError('Select an exact registered session ID')
        if not isinstance(idempotency_key, str) or not KEY.fullmatch(idempotency_key):
            raise ValueError('Use a stable 16..128 character idempotency key')
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise ValueError('Handoff text must contain 1..2000 characters')
        configured = [item for item in self.contexts if item.get('id') == context_id and item.get('enabled')]
        if len(configured) == 1 and configured[0].get('provider') != 'opencode':
            return {'outcome': 'copyable', 'reason': 'provider_delivery_unsupported',
                    'context_id': context_id, 'handoff': text}
        # Reconcile the stable key before retry. The API owns idempotency and delivery state.
        try:
            prior = self.request('GET', '/api/instructions/by-key/'+idempotency_key)
        except LookupError:
            prior = None
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {401, 403}:
                return {'outcome': 'denied', 'reason': 'operator_api_denied'}
            return {'outcome': 'uncertain', 'reason': 'reconciliation_unavailable'}
        except Exception:
            return {'outcome': 'uncertain', 'reason': 'reconciliation_unavailable'}
        if prior is not None:
            if (prior.get('registered_session_id') != expected_session_id or
                    prior.get('text') != text.strip() or prior.get('expiry_minutes') != 15):
                return {'outcome': 'denied', 'reason': 'idempotency_conflict'}
            return self._receipt(prior)
        target = self.target(context_id)
        if target.get('outcome') in {'denied', 'uncertain'}:
            return target
        if target.get('outcome') != 'ready' or target['session_id'] != expected_session_id:
            return {'outcome': 'copyable', 'reason': 'target_changed_or_unavailable',
                    'context_id': context_id, 'handoff': text}
        try:
            row = self.request('POST', '/api/instructions', {
                'registered_session_id': expected_session_id,
                'idempotency_key': idempotency_key, 'text': text,
                'expiry_minutes': 15})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {401, 403, 409, 422, 429}:
                return {'outcome': 'denied', 'reason': 'queue_rejected'}
            return {'outcome': 'uncertain', 'reason': 'submission_outcome_unknown',
                    'idempotency_key': idempotency_key}
        except Exception:
            return {'outcome': 'uncertain', 'reason': 'submission_outcome_unknown',
                    'idempotency_key': idempotency_key}
        if row.get('registered_session_id') != expected_session_id:
            return {'outcome': 'uncertain', 'reason': 'receipt_target_mismatch',
                    'idempotency_key': idempotency_key}
        return self._receipt(row)

    @staticmethod
    def _receipt(row):
        state = row.get('state')
        outcome = ('accepted' if state in {'queued', 'claimed'} else
                   'received' if state in {'received', 'responded'} else
                   'not_delivered' if state in {'failed', 'expired'} else 'uncertain')
        return {'outcome': outcome, 'instruction_id': row.get('id'),
                'session_id': row.get('registered_session_id'), 'state': state,
                'created_at': row.get('created_at'), 'updated_at': row.get('updated_at')}
