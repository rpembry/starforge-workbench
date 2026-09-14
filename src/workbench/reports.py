"""Read-only report views; original legacy date precision governs standup windows."""
import json
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

DEFAULT_ZONE = ZoneInfo('America/New_York')
LEGACY_IMPORT_ZONE = DEFAULT_ZONE


def standup_window(current, zone=DEFAULT_ZONE):
    current=current.astimezone(zone)
    end=datetime.combine(current.date(),time(9),zone)
    if current<end:
        end-=timedelta(days=1)
    while end.weekday()>=5:
        end-=timedelta(days=1)
    start=end-timedelta(days=1)
    while start.weekday()>=5:
        start-=timedelta(days=1)
    return start,end


def event_time(event, metadata, zone=DEFAULT_ZONE):
    original=metadata.get(event['id'],{}).get('occurred_at')
    if original and len(original)==10:
        # Match legacy reports without pretending a date-only event happened at midnight.
        return datetime.combine(datetime.fromisoformat(original).date(),time(12),LEGACY_IMPORT_ZONE)
    return datetime.fromisoformat(event['occurred_at']).astimezone(zone)


def report(repo,kind,current=None,zone=DEFAULT_ZONE):
    current=current or datetime.now(zone)
    with repo.connection() as db:
        events=[dict(r) for r in db.execute("SELECT * FROM events WHERE kind='accomplishment' ORDER BY occurred_at,id")]
        metadata={r['target_id']:json.loads(r['metadata']) for r in db.execute("SELECT target_id,metadata FROM import_records WHERE target_resource='events'")}
        actions=[dict(r) for r in db.execute("SELECT * FROM actions WHERE status IN ('accepted','in_progress','waiting','approval_needed') ORDER BY CASE status WHEN 'in_progress' THEN 0 ELSE 1 END,priority,updated_at DESC")]
    start,end=standup_window(current, zone)
    if kind=='standup':
        events=[e for e in events if start<=event_time(e,metadata,zone)<=end]
    elif kind=='todo':
        events=[]
    else:
        events.reverse()
    result = dict(kind=kind,generated_at=current.isoformat(),timezone=zone.key,
                window={'start':start.isoformat(),'end':end.isoformat()} if kind=='standup' else None,
                accomplishments=events,
                plan=[a for a in actions if a['status'] in {'accepted','in_progress'}][:12] if kind=='standup' else [a for a in actions if a['status'] in {'accepted','in_progress'}],
                waiting=[a for a in actions if a['status'] in {'waiting','approval_needed'}],
                note='Quarantined inferred tasks are excluded. Automated Codex observations are attributed reports, not independently verified completion.')

    from .report_suggestions import view
    result['report_suggestions'] = view(repo, kind, result)
    return result
