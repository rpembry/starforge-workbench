"""Presentation derived from immutable events; never infer task completion."""
import json
from datetime import datetime


ACTION_LABELS = {
    'observed': 'Observed', 'proposed': 'Proposed', 'accepted': 'Accepted',
    'in_progress': 'Started', 'waiting': 'Waiting', 'approval_needed': 'Approval requested',
    'done': 'Completed', 'rejected': 'Rejected', 'canceled': 'Canceled',
}


def present(events, actions, generated_at):
    by_id = {action['id']: action for action in actions}
    current = datetime.fromisoformat(generated_at)
    result = []
    for event in events:
        action = by_id.get(event['action_id'])
        title = event['summary']
        if event['kind'] == 'action_transition' and event['source'] == 'workbench-api' and action:
            status = event['summary'].removeprefix('Action ')
            if event['summary'].startswith('Action ') and status in ACTION_LABELS:
                label = ACTION_LABELS[status]
                if event.get('details'):
                    try:
                        change = json.loads(event['details'])
                    except (ValueError, TypeError):
                        change = None
                    if not isinstance(change, dict) or change.get('from') == change.get('to'):
                        label = 'Updated'
                title = label+': '+action['title']
        if event['source'] == 'collector-health' and title.startswith('Collector '):
            source, separator, status = title[len('Collector '):].rpartition(': ')
            if separator and status in {'ok', 'degraded'}:
                title = ('Collector healthy: ' if status == 'ok' else 'Collector needs attention: ')+source
        age = (current-datetime.fromisoformat(event['occurred_at'])).total_seconds()
        if age < 0:
            relative = 'Future-dated record'
        elif age < 60:
            relative = 'Just now'
        elif age < 3600:
            count = int(age//60)
            relative = f'{count} minute'+('s' if count != 1 else '')+' ago'
        elif age < 86400:
            count = int(age//3600)
            relative = f'{count} hour'+('s' if count != 1 else '')+' ago'
        else:
            count = int(age//86400)
            relative = f'{count} day'+('s' if count != 1 else '')+' ago'
        result.append({**event, 'display_title': title, 'relative_time': relative,
                       'display_project': event['project'] or (action['project'] if action else None)})
    return result
