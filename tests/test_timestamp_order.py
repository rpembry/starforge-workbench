"""Mixed-offset instants must remain sortable in live and upgraded databases."""
import sqlite3

import pytest

from test_api import api, repo
from workbench.repository import SQLiteRepository


def test_mixed_offset_events_sort_by_instant_in_every_view(api):
    samples = [('e0', '2026-10-05T10:00:00+00:00'),
               ('e1', '2026-10-05T08:00:00-04:00'),
               ('e2', '2026-10-05T11:00:00Z')]
    for name, stamp in samples:
        result = api.post('/api/events', json={'source': 'fixture', 'source_id': name,
            'kind': 'accomplishment', 'summary': name, 'occurred_at': stamp})
        assert result.status_code == 201, result.text
    expected = ['e1', 'e2', 'e0']
    events = api.get('/api/events').json()['items']
    recent = api.get('/api/dashboard').json()['recent']
    report = api.get('/api/reports/accomplishments').json()['accomplishments']
    assert [item['summary'] for item in events] == expected
    assert [item['summary'] for item in recent] == expected
    assert [item['summary'] for item in report] == expected
    assert [item['occurred_at'] for item in events] == [
        '2026-10-05T12:00:00.000000+00:00',
        '2026-10-05T11:00:00.000000+00:00',
        '2026-10-05T10:00:00.000000+00:00']


def test_existing_events_are_rewritten_without_losing_immutable_guards(repo):
    with repo.connection() as db:
        db.execute('DROP TRIGGER events_immutable_update')
        db.execute("""INSERT INTO events(id,kind,summary,details,project,action_id,run_id,
                    source,source_id,occurred_at,recorded_at,recorded_by)
                    VALUES('old','observation','Old event','',NULL,NULL,NULL,
                           'fixture','old','2026-10-05T08:00:00-04:00',
                           '2026-10-05T12:01:00Z','fixture')""")
        db.execute("""CREATE TRIGGER events_immutable_update BEFORE UPDATE ON events
                    BEGIN SELECT RAISE(ABORT, 'Events are immutable'); END""")
        db.execute('DELETE FROM schema_migrations WHERE version IN (13,14)')
        db.commit()
    upgraded = SQLiteRepository(repo.path)
    event = upgraded.get('events', 'old')
    assert event['occurred_at'] == '2026-10-05T12:00:00.000000+00:00'
    assert event['recorded_at'] == '2026-10-05T12:01:00.000000+00:00'
    with upgraded.connection() as db, pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE events SET summary='changed' WHERE id='old'")
