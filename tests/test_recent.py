from datetime import datetime, timedelta, timezone
from workbench.recent import present
from test_api import api, repo, action


def test_recent_uses_historical_transition_and_current_subject_without_rewriting_events(api, repo):
    record = action(api, title='Review fixture', status='accepted', project='Example')
    api.post('/api/actions/'+record['id']+'/transitions', json={'version': 1, 'transition': 'complete'})
    before = repo.list('events')
    recent = api.get('/api/dashboard').json()['recent']
    assert {event['display_title'] for event in recent} == {'Accepted: Review fixture', 'Completed: Review fixture'}
    assert all(event['display_project'] == 'Example' for event in recent)
    assert repo.list('events') == before
    api.patch('/api/actions/'+record['id'], json={'version': 2, 'title': 'Renamed fixture'})
    labels = [event['display_title'] for event in api.get('/api/dashboard').json()['recent']]
    assert labels.count('Completed: Renamed fixture') == 1
    assert 'Updated: Renamed fixture' in labels
    assert {event['summary'] for event in before} == {'Action accepted', 'Action done'}


def test_non_transition_and_missing_subject_preserve_attributed_summary():
    current = datetime.now(timezone.utc)
    event = dict(id='fixture', summary='Agent reports task complete', kind='observation', source='fixture',
                 action_id=None, project=None, occurred_at=(current-timedelta(minutes=2)).isoformat())
    assert present([event], [], current.isoformat())[0]['display_title'] == event['summary']
    assert present([event], [], current.isoformat())[0]['relative_time'] == '2 minutes ago'
    event.update(kind='action_transition', source='workbench-api', action_id='missing', summary='Action done')
    assert present([event], [], current.isoformat())[0]['display_title'] == 'Action done'
    event['occurred_at'] = (current+timedelta(minutes=2)).isoformat()
    assert present([event], [], current.isoformat())[0]['relative_time'] == 'Future-dated record'


def test_recent_html_escapes_titles_and_keeps_raw_details_in_collapsed_disclosure(api):
    record = action(api, title='<script>unsafe</script>', status='accepted')
    api.post('/api/actions/'+record['id']+'/transitions', json={'version': 1, 'transition': 'complete'})
    html = api.get('/').text.split('<section id="recent">')[1]
    assert 'Completed: &lt;script&gt;unsafe&lt;/script&gt;' in html
    assert '<script>unsafe</script>' not in html
    assert '<details><summary>Details and provenance</summary>' in html
    assert '<details open' not in html
    assert html.index('<details>') < html.index('changed_fields')
    assert 'Source reference:' in html and 'Just now' in html


def test_collector_initial_health_does_not_claim_recovery():
    event = dict(id='health', summary='Collector fixture: ok', kind='observation', source='collector-health',
                 action_id=None, project=None, occurred_at='2026-09-12T10:00:00+00:00')
    assert present([event], [], '2026-09-12T11:00:00+00:00')[0]['display_title'] == 'Collector healthy: fixture'
