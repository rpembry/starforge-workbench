"""Presentation of registered-session evidence without provider content."""
from datetime import datetime, timezone

from .repository import Problem


EVIDENCE_LABELS = {
    'present': 'Present',
    'attention_needed': 'Needs input',
    'provider_error': 'Provider error observed',
    'stopped': 'Stopped',
    'unknown': 'Unknown',
}
REASON_LABELS = {
    'process_observed': 'Registered process observed',
    'permission_wait': 'Permission request observed',
    'user_question': 'Question observed',
    'provider_error': 'Provider error observed',
    'process_stopped': 'Registered process stopped',
    'registration_replaced': 'Exact registered process or binding was replaced',
    'exact_binding_missing': 'Exact provider binding unavailable; session is not controllable',
    'no_evidence': 'No stronger evidence available',
}
INSTRUCTION_STATES = {
    'queued': ('Queued', 'Waiting for the owning workstation. Provider receipt is not established.'),
    'claimed': ('Claimed', 'The owning worker has a short lease. Provider receipt is not established.'),
    'received': ('Received by provider', 'The provider accepted the instruction. This is not evidence that the requested work completed.'),
    'responded': ('Response observed', 'Correlated provider output was observed. This is not evidence that the requested work succeeded or completed.'),
    'failed': ('Delivery failed', 'A definite failure was reported before provider acceptance.'),
    'expired': ('Expired', 'The instruction expired before provider acceptance was established.'),
    'uncertain': ('Delivery uncertain', 'Delivery may have crossed the provider boundary. It will not be sent automatically again.'),
}
INSTRUCTION_REASONS = {
    'operator_created': 'Created by operator',
    'worker_claimed': 'Claimed by owning worker',
    'provider_unavailable': 'Provider unavailable before transmission',
    'session_busy': 'Provider could not safely accept the instruction yet',
    'provider_accepted': 'Provider acceptance observed',
    'provider_response_error': 'Correlated provider error output observed',
    'provider_response_without_error': 'Correlated provider output completed without an observed error',
    'provider_rejected': 'Provider rejected the instruction',
    'session_missing': 'Exact provider session was not found',
    'unsupported_provider': 'Provider is not supported',
    'acknowledgement_lost': 'Provider acknowledgement was lost',
    'delivery_ambiguous': 'Provider acceptance could not be determined',
    'worker_interrupted': 'Worker stopped during an ambiguous attempt',
    'instruction_expired': 'Instruction expiry reached',
    'lease_expired': 'Worker lease ended without safe delivery evidence',
}


def age_label(stamp, current=None):
    if not stamp:
        return 'Unknown'
    current = current or datetime.now(timezone.utc)
    try:
        age = (current - datetime.fromisoformat(stamp)).total_seconds()
    except (TypeError, ValueError):
        return 'Unknown'
    if age < -60:
        return 'Unknown'
    seconds = max(0, int(age))
    if seconds < 60:
        return 'Less than a minute ago'
    if seconds < 3600:
        return f'{seconds // 60} minute' + (' ago' if seconds // 60 == 1 else 's ago')
    if seconds < 86400:
        return f'{seconds // 3600} hour' + (' ago' if seconds // 3600 == 1 else 's ago')
    return f'{seconds // 86400} day' + (' ago' if seconds // 86400 == 1 else 's ago')


def display_session(row, repository, current=None):
    """Decorate a bounded registration using only verified local references."""
    current = current or datetime.now(timezone.utc)
    item = dict(row)
    visibility = row['visibility']
    if visibility == 'offline':
        item['status_label'] = 'Offline visibility'
        item['reason_label'] = 'No recent registration heartbeat; session state is unknown.'
    elif visibility == 'stale':
        item['status_label'] = 'Stale visibility'
        item['reason_label'] = 'Registration heartbeat is late; session state is unknown.'
    else:
        item['status_label'] = EVIDENCE_LABELS.get(row['evidence_state'], 'Unknown')
        item['reason_label'] = REASON_LABELS.get(row['reason'], 'No further reason available')
    item['activity_label'] = age_label(row['last_activity_at'], current)
    item['heartbeat_label'] = age_label(row['heartbeat_at'], current)
    item['action_title'] = None
    item['objective_title'] = None
    if row['run_id'] and row['action_id']:
        try:
            run = repository.get('runs', row['run_id'])
            if run['action_id'] == row['action_id']:
                action = repository.get('actions', row['action_id'])
                item['action_title'] = action['title']
                if action['objective_id']:
                    item['objective_title'] = repository.get('objectives', action['objective_id'])['title']
        except Problem as exc:
            if exc.status != 404:
                raise
            # Keep the session visible when a linked record cannot be resolved.
            # Never invent an association from the stored IDs alone.
    item['send_allowed'] = (visibility == 'fresh' and row['provider'] == 'opencode'
                            and row['evidence_state'] not in {'unknown', 'stopped'})
    if item['send_allowed']:
        item['send_explanation'] = 'The server will queue plain text for this exact registered session.'
    elif visibility != 'fresh':
        item['send_explanation'] = 'Instructions cannot be queued while session visibility is stale or offline.'
    else:
        item['send_explanation'] = 'This registration is not currently a controllable OpenCode target.'
    return item


def timestamp_label(stamp):
    try:
        value = datetime.fromisoformat(stamp).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return 'Unknown time'
    return value.strftime('%Y-%m-%d %H:%M UTC')


def display_instruction(row):
    """Present bounded operator-visible delivery evidence without implying completion."""
    item = dict(row)
    item['state_label'], item['state_explanation'] = INSTRUCTION_STATES.get(
        row['state'], ('Unknown', 'No recognized delivery evidence is available.'))
    item['created_label'] = timestamp_label(row['created_at'])
    item['expires_label'] = timestamp_label(row['expires_at'])
    item['history'] = [dict(event,
                            state_label=INSTRUCTION_STATES.get(event['state'], ('Unknown', ''))[0],
                            reason_label=INSTRUCTION_REASONS.get(event['reason_code'], 'Delivery state updated'),
                            occurred_label=timestamp_label(event['occurred_at']))
                       for event in row.get('history', [])]
    return item
