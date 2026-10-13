"""One bounded attention projection for API, status, and dashboard views."""
from datetime import datetime, timedelta, timezone

from .attention import derive


def prepare(actions, runs, collectors, incidents, current):
    for run in runs:
        run['stale'] = (current - datetime.fromisoformat(run['heartbeat_at'])).total_seconds() > 90
    for collector in collectors:
        age = (current - datetime.fromisoformat(collector['heartbeat_at'])).total_seconds()
        collector['health'] = 'offline' if age > 90 else collector['status']
    for incident in incidents:
        age = (current - datetime.fromisoformat(incident['last_observed_at'])).total_seconds()
        incident['fresh'] = bool(incident['reason']) and age <= 90
    return derive(actions, runs, collectors, current.isoformat(), incidents)


def read(db, current=None):
    """Skip terminal history while retaining current approvals and linked evidence."""
    current = current or datetime.now(timezone.utc)
    cutoff = (current - timedelta(seconds=90)).isoformat()
    actions = [dict(row) for row in db.execute(
        "SELECT * FROM actions WHERE status IN ('accepted','approval_needed')")]
    runs = {row['id']: dict(row) for row in db.execute('''SELECT * FROM runs
        WHERE status='approval_needed' OR
              (status IN ('running','waiting') AND heartbeat_at>=?)''', (cutoff,))}
    # At most three previous runs per active agent action are shown as evidence.
    # The action/heartbeat index avoids visiting runs of terminal actions.
    for action in actions:
        if action['status'] == 'accepted' and action['execution_mode'] == 'agent':
            for row in db.execute('''SELECT * FROM runs WHERE action_id=?
                ORDER BY heartbeat_at DESC LIMIT 3''', (action['id'],)):
                runs.setdefault(row['id'], dict(row))
    collectors = [dict(row) for row in db.execute('SELECT * FROM collectors ORDER BY source')]
    incidents = [dict(row) for row in db.execute('''SELECT i.*,g.generation_started_at
        FROM provider_attention_incidents i JOIN provider_attention g
          ON g.provider=i.provider AND g.session_id=i.session_id AND g.generation_id=i.generation_id
        WHERE i.state='open' ORDER BY i.provider,i.session_id,i.opened_at,i.incident_id''')]
    return prepare(actions, sorted(runs.values(), key=lambda row: row['heartbeat_at'], reverse=True),
                   collectors, incidents, current)
