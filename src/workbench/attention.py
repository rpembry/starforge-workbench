"""Read-only attention rules. Missing evidence never proves failure."""
from datetime import datetime, timedelta

ACTIVE = {'running', 'waiting', 'approval_needed'}


def evidence(resource, row, fields):
    return dict(resource=resource, id=row['id'],
                timestamps={key: row.get(key) for key in fields})


def derive(actions, runs, collectors, generated_at, provider_attention=()):
    by_action = {a['id']: a for a in actions}
    linked = {}
    for run in runs:
        if run['action_id']:
            linked.setdefault(run['action_id'], []).append(run)
    items = {}

    provider_reasons = {
        'permission_wait': ('provider_permission_wait', 0, 'waiting',
            'OpenCode reported that the current turn is waiting for permission.',
            'Review the permission request in the matching OpenCode session before replying.'),
        'user_question': ('provider_user_question', 0, 'waiting',
            'OpenCode reported that the current turn is waiting for an answer.',
            'Review the question in the matching OpenCode session before replying.'),
        'provider_error': ('provider_error', 1, 'blocked',
            'OpenCode reported a provider error for the current turn.',
            'Inspect the matching OpenCode session and provider status; do not infer task failure.'),
    }
    for observation in provider_attention:
        if observation['reason'] not in provider_reasons:
            continue
        kind, priority, progress, reason, next_action = provider_reasons[observation['reason']]
        identity = 'provider_attention:'+observation['provider']+':'+observation['session_id']+':'+observation['incident_id']
        fresh_until = (datetime.fromisoformat(observation['last_observed_at'])+timedelta(seconds=90)).isoformat()
        if not observation['fresh']:
            progress = 'unknown'
            reason = reason.rstrip('.')+' was observed, but current status is unverified; review the matching session.'
        items[identity] = dict(id=identity, kind=kind, priority=priority,
            title='OpenCode session', subject={'resource': 'provider_attention', 'id': observation['session_id']},
            progress=progress, reason=reason, next_action=next_action,
            evidence=[dict(resource='provider_attention', id=observation['session_id'],
                generation_id=observation['generation_id'], generation_started_at=observation['generation_started_at'],
                incident_id=observation['incident_id'], sequence=observation['last_sequence'],
                provenance=observation['open_provenance'],
                timestamps={'observed_at': observation['last_observed_at'], 'fresh_until': fresh_until})])

    def approval(action=None, run=None):
        resource, row = ('actions', action) if action else ('runs', run)
        identity = resource+':'+row['id']
        if identity not in items:
            items[identity] = dict(id=identity, kind='approval_needed', priority=0,
                title=action['title'] if action else run['context'],
                subject={'resource': resource, 'id': row['id']},
                progress='needs_review', reason='An explicit approval request needs your decision.',
                next_action='Ask Workbench to review this approval and its current evidence before approving or rejecting it.',
                evidence=[])
        item = items[identity]
        record = evidence(resource, row, ['created_at', 'updated_at'] if action else ['started_at', 'heartbeat_at', 'last_activity_at'])
        if record not in item['evidence']:
            item['evidence'].append(record)
        if run and action:
            item['evidence'].append(evidence('runs', run, ['started_at', 'heartbeat_at', 'last_activity_at']))
        if run and run['stale']:
            item['progress'] = 'unknown'
            item['reason'] = 'An approval request remains recorded, but linked run contact is stale. Current progress is unknown; confirm the request is still pending.'

    for action in actions:
        if action['status'] == 'approval_needed':
            approval(action=action)
    for run in runs:
        if run['status'] == 'approval_needed':
            approval(action=by_action.get(run['action_id']), run=run)
    for collector in collectors:
        if collector['health'] == 'ok':
            continue
        identity = 'collectors:'+collector['id']
        reason = ('No heartbeat for more than 90 seconds.' if collector['health'] == 'offline'
                  else 'Collector reports degraded health ('+collector['reason']+').')
        items[identity] = dict(id=identity, kind='collector_health', priority=1,
            title=collector['source'], subject={'resource': 'collectors', 'id': collector['id']},
            progress='unknown', reason=reason+' Agent activity is unknown where collection is incomplete; this does not establish agent failure.',
            next_action='Ask Workbench to check this collector service and its last successful scan; restore collection before judging agent progress.',
            evidence=[evidence('collectors', collector, ['heartbeat_at', 'last_success_at'])])
    for action in actions:
        identity = 'actions:'+action['id']
        if identity in items or action['status'] != 'accepted' or action['execution_mode'] != 'agent':
            continue
        related = linked.get(action['id'], [])
        if any(not r['stale'] and r['status'] in ACTIVE for r in related):
            continue
        items[identity] = dict(id=identity, kind='agent_without_active_run', priority=2,
            title=action['title'], subject={'resource': 'actions', 'id': action['id']}, progress='unknown',
            reason='This accepted agent action has no fresh active linked run. Work may be unlinked, unobserved, or not started; failure is not established.',
            next_action='Ask Workbench to confirm the assigned agent is working and link its current run, or explicitly start or reschedule the action.',
            evidence=[evidence('actions', action, ['created_at', 'updated_at'])]+
                     [evidence('runs', r, ['started_at', 'heartbeat_at', 'last_activity_at']) for r in related[:3]])
    return dict(generated_at=generated_at,
                items=sorted(items.values(), key=lambda i: (i['priority'], i['id'])),
                note='Read-only attention signals. Suggestions are separate; no task is committed or executed by these rules.')
