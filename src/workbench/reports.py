"""Read-only report views; original legacy date precision governs standup windows."""
import json
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

ZONE=ZoneInfo('America/New_York')


def standup_window(current):
    current=current.astimezone(ZONE)
    end=datetime.combine(current.date(),time(9),ZONE)
    if current<end:
        end-=timedelta(days=1)
    while end.weekday()>=5:
        end-=timedelta(days=1)
    start=end-timedelta(days=1)
    while start.weekday()>=5:
        start-=timedelta(days=1)
    return start,end


def event_time(event, metadata):
    original=metadata.get(event['id'],{}).get('occurred_at')
    if original and len(original)==10:
        # Match legacy reports without pretending a date-only event happened at midnight.
        return datetime.combine(datetime.fromisoformat(original).date(),time(12),ZONE)
    return datetime.fromisoformat(event['occurred_at']).astimezone(ZONE)


def report(repo,kind,current=None):
    current=current or datetime.now(ZONE)
    with repo.connection() as db:
        events=[dict(r) for r in db.execute("SELECT * FROM events WHERE kind='accomplishment' ORDER BY occurred_at,id")]
        metadata={r['target_id']:json.loads(r['metadata']) for r in db.execute("SELECT target_id,metadata FROM import_records WHERE target_resource='events'")}
        actions=[dict(r) for r in db.execute("SELECT * FROM actions WHERE status IN ('accepted','in_progress','waiting','approval_needed') ORDER BY CASE status WHEN 'in_progress' THEN 0 ELSE 1 END,priority,updated_at DESC")]
    start,end=standup_window(current)
    if kind=='standup':
        events=[e for e in events if start<=event_time(e,metadata)<=end]
    elif kind=='todo':
        events=[]
    else:
        events.reverse()
    return dict(kind=kind,generated_at=current.isoformat(),timezone='America/New_York',
                window={'start':start.isoformat(),'end':end.isoformat()} if kind=='standup' else None,
                accomplishments=events,
                plan=[a for a in actions if a['status'] in {'accepted','in_progress'}][:12] if kind=='standup' else [a for a in actions if a['status'] in {'accepted','in_progress'}],
                waiting=[a for a in actions if a['status'] in {'waiting','approval_needed'}],
                note='Quarantined inferred tasks are excluded. Automated Codex observations are attributed reports, not independently verified completion.')
