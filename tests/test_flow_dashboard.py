from test_api import COLLECTOR, OPERATOR, action, api, repo

from test_flow_relations import WORK_ITEM, publication


def test_flow_page_is_read_only_escapes_content_and_keeps_states_separate(api, repo):
    accepted = action(api, title='Operator accepted', status='accepted')
    api.post('/api/flow/work-items', json=publication(task_ids=['step-one']))
    api.post(f'/api/flow/work-items/{WORK_ITEM}/actions/{accepted["id"]}/link',
             json={'operation_id': 'link-page', 'work_item_version': 1,
                   'action_version': accepted['version']})
    with repo.connection() as db:
        db.execute('UPDATE flow_work_items SET task_ids=?,source_ref=? WHERE work_item_id=?',
                   ('["<script>alert(1)</script>"]', 'javascript:alert(1)', WORK_ITEM))
        db.commit()
    html = api.get('/flow').text
    assert '&lt;script&gt;' in html and '<script>alert(1)</script>' not in html
    assert 'href="javascript:' not in html
    assert 'Source link unavailable' in html
    assert 'Operator accepted' not in html
    assert 'outcome and blockers unknown' in html
    assert len(api.get('/api/actions').json()['items']) == 1
    api.headers['Authorization'] = 'Bearer ' + COLLECTOR
    assert api.get('/flow').status_code == 403
    api.headers['Authorization'] = 'Bearer ' + OPERATOR
    assert api.get('/flow?offset=-1').status_code == 422


def test_flow_empty_page_is_truthful(api):
    assert 'Local work may exist' in api.get('/flow').text


def test_opt_in_summary_renders_blocker_safely(api):
    body = publication(task_ids=['step-one'], task_summaries=[{
        'task_id': 'step-one', 'title': '<img src=x onerror=alert(1)>',
        'blocked': True, 'review_pending': True}])
    assert api.post('/api/flow/work-items', json=body).status_code == 201
    html = api.get('/flow').text
    assert '&lt;img' in html and '<img src=x' not in html
    assert 'blocked' in html and 'review pending' in html
    assert api.post('/api/flow/work-items', json=publication(
        operation_id='bad-summary', work_item_id='wi-' + 'd' * 32,
        source_ref='https://example.org/work-items/another', task_ids=[],
        task_summaries=[{'task_id': 'step-one', 'title': 'Unknown',
                         'blocked': False, 'review_pending': False}])).status_code == 422


def test_flow_guide_and_pagination_are_read_only(api):
    guide = api.get('/guide').text
    assert 'ai-workbench tasks list ISSUE' in guide
    assert 'optional integrations' in guide
    for index in range(26):
        api.post('/api/flow/work-items', json=publication(
            operation_id=f'page-{index}', work_item_id='wi-' + f'{index:032x}',
            source_ref=f'https://example.com/work-items/{index}'))
    first = api.get('/flow')
    assert first.status_code == 200 and 'offset=25' in first.text
    second = api.get('/flow?offset=25')
    assert 'offset=0' in second.text
    assert second.text.count('<article>') == 1
    assert api.get('/api/flow/work-items').json()['items']
