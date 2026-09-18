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
    return item
