from copy import deepcopy
import json
from pathlib import Path

import httpx
import pytest

pytest.importorskip('bs4', reason='Install optional newsletter extra for converter tests')
from workbench.newsletter_inline import ConversionError, convert, source_matches, safe_link, VERSION
from workbench.newsletter_job import Remote, JobError, process, digest, once, protected_read, self_links, description_hash, validate_state

HTML = '''<html><body><div style="display: none">hidden preheader</div>
<h1>TLDR Dev 2026-01-02</h1><table width="600"><tr><td>
<h1>Articles &amp; Tutorials</h1><a href="https://tldr.tech/dev">Sign up</a>
<p><strong>Readable article</strong><br>Full body with literal [brackets] and *stars*.</p>
<p><a href="https://example.com/article">Article link</a></p>
<p><a href="javascript:alert(1)">Unsafe label stays readable</a></p>
<img src="https://example.com/pixel" style="display:none"><img alt="Sponsor logo" src="https://example.com/logo">
<strong><h1>Miscellaneous</h1></strong><p>Final body paragraph.</p>
</td></tr></table><script>exfiltrate()</script></body></html>'''
PROJECT = 'exampleProject'


def task(i='task1'):
    return {'id': i, 'project_id': PROJECT, 'content': 'Newsletter title', 'description': '', 'is_completed': False}


def comment(i='task1'):
    return {'id': 'comment1', 'task_id': i, 'attachment': {'file_name': 'Newsletter title', 'file_type': 'text/html', 'file_url': 'https://files.todoist.com/user_upload/example/file.html'}}


class FakeRemote:
    def __init__(self, tasks=None):
        self.items = tasks or [task()]
        self.calls = []
        self.crash = False
        self.concurrent_edit = False

    def tasks(self, project):
        return deepcopy(self.items)

    def comments(self, task_id):
        return [comment(task_id)]

    def html(self, url):
        return HTML

    def task(self, task_id):
        item = next(t for t in self.items if t['id'] == task_id)
        if self.concurrent_edit:
            item['description'] = 'User edited this'
        return deepcopy(item)

    def update(self, task_id, description):
        self.calls.append(('update', task_id, description))
        next(t for t in self.items if t['id'] == task_id)['description'] = description
        if self.crash:
            raise JobError('write_unknown')


def run(remote, state=None, apply=True, **config):
    state = state if state is not None else {'receipts': {}}
    saves = []
    def save(payload, **kwargs): saves.append((deepcopy(payload), kwargs))
    counts = process(remote, {'project_id': PROJECT, **config}, state, save, apply=apply)
    return counts, state, saves


def test_full_text_safe_links_headings_and_no_tracking():
    out = convert(HTML)
    assert 'Full body with literal \\[brackets\\] and \\*stars\\*.' in out.description
    assert 'Final body paragraph.' in out.description
    assert '## Miscellaneous' in out.description and '**##' not in out.description
    assert '[Article link](https://example.com/article)' in out.description
    for denied in ('javascript:', 'hidden preheader', 'exfiltrate', '<table', '<img', 'example.com/pixel'):
        assert denied not in out.description
    assert 'Unsafe label stays readable' in out.description
    assert 'Sponsor logo' in out.description
    assert out.headings == 3 and out.links == 2


def test_self_link_unfurling_preserves_url_and_article_checks():
    expected = '[https://example.com/](https://example.com/)\n[Article](https://example.com/read)'
    unfurled = '[Example homepage](https://example.com/)\n[Article](https://example.com/read)'
    urls = self_links(expected)
    assert description_hash(expected, urls) == description_hash(unfurled, urls)
    assert description_hash(expected, urls) != description_hash(unfurled.replace('[Article]', '[Edited article]'), urls)
    assert description_hash(expected, urls) != description_hash(unfurled + '\nUser edit', urls)
    assert description_hash(expected, urls) != description_hash(unfurled.replace('https://example.com/read', 'https://example.com/other'), urls)
    assert self_links('[example.com](http://example.com)') == ['http://example.com']


@pytest.mark.parametrize('link', ['javascript:bad()', 'data:text/html,bad', 'file:///secret', '//example.com', 'https://user:secret@example.com', 'https://example.com/\nheader'])
def test_unsafe_links(link):
    assert safe_link(link) is None


def test_known_redirect_decoded_without_network():
    assert safe_link('https://tracking.tldrnewsletter.com/CL0/https:%2F%2Fexample.com%2Farticle/1/opaque') == 'https://example.com/article'
    assert safe_link('https://other.example.com/CL0/https:%2F%2Fexample.com/1/opaque').startswith('https://other.example.com/')
    assert safe_link('https://tracking.tldrnewsletter.com/CL0/https:%2F%2Fexample.com%2F%0Abad/1/opaque') is None


def test_limits_fail_without_truncation():
    with pytest.raises(ConversionError, match='description_limit'):
        convert('<html><body>' + '👓' * 9000 + '</body></html>')
    with pytest.raises(ConversionError, match='source_too_large'):
        convert('x' * 2_000_001)
    with pytest.raises(ConversionError, match='missing_body'):
        convert('plain text')


def test_literal_source_cannot_become_html_or_quote_markup():
    out = convert('<html><body><p>&gt;&gt; Read this</p><p>&lt;script&gt;literal&lt;/script&gt;</p><p># Literal heading</p></body></html>')
    assert '\\>\\> Read this' in out.description
    assert '\\<script>' in out.description and '\\</script>' in out.description
    assert '\\# Literal heading' in out.description


@pytest.mark.parametrize('change', ['project', 'task_id', 'filename', 'description', 'completed', 'no_signature'])
def test_exact_matching_and_user_preservation(change):
    t, c, html = task(), comment(), HTML
    if change == 'project': t['project_id'] = 'other'
    if change == 'task_id': c['task_id'] = 'other'
    if change == 'filename': c['attachment']['file_name'] = 'Other title'
    if change == 'description': t['description'] = 'User notes'
    if change == 'completed': t['is_completed'] = True
    if change == 'no_signature': html = '<html><body><p>Ordinary notes</p></body></html>'
    assert not source_matches(t, c, html, PROJECT)


def test_backup_and_intent_precede_update_and_idempotency():
    remote = FakeRemote()
    counts, state, saves = run(remote)
    assert counts['updated'] == 1
    assert saves[0][1] == {'task_id': 'task1'} and saves[0][0]['html'] == HTML
    assert saves[1][0]['receipts']['task1']['status'] == 'sending'
    assert state['receipts']['task1']['status'] == 'verified'
    counts, _, _ = run(remote, state)
    assert counts['updated'] == 0 and len(remote.calls) == 1
    remote.items[0]['description'] = ''
    counts, _, _ = run(remote, state)
    assert counts['updated'] == 0 and len(remote.calls) == 1


def test_changed_description_skipped_at_last_read():
    remote = FakeRemote(); remote.concurrent_edit = True
    counts, state, _ = run(remote)
    assert counts['skipped'] == 1 and not remote.calls and not state['receipts']


def test_uncertain_write_exact_reconciliation_no_resend():
    remote = FakeRemote(); remote.crash = True
    counts, state, _ = run(remote)
    assert counts['failed'] == 1 and state['receipts']['task1']['status'] == 'sending'
    counts, state, _ = run(remote, state)
    assert state['receipts']['task1']['status'] == 'verified' and len(remote.calls) == 1


def test_unknown_write_with_different_remote_content_held():
    remote = FakeRemote()
    state = {'receipts': {'task1': {'status': 'sending', 'description_hash': digest('intended')}}}
    counts, state, _ = run(remote, state)
    assert counts['failed'] and not remote.calls and state['receipts']['task1']['status'] == 'sending'


def test_dry_run_does_not_save_or_update():
    remote = FakeRemote()
    counts, state, saves = run(remote, apply=False)
    assert counts['pending'] == 1 and not remote.calls and not saves
    state['receipts']['task1'] = {'status': 'sending', 'description_hash': digest('')}
    counts, state, saves = run(remote, state, apply=False)
    assert state['receipts']['task1']['status'] == 'sending' and not saves


def test_run_cap_and_duplicate_ids():
    remote = FakeRemote([task('task1'), task('task1'), task('task2')])
    counts, state, _ = run(remote, max_tasks=1)
    assert counts['updated'] == 1 and counts['pending'] == 1 and len(remote.calls) == 1


def test_failed_source_has_persisted_backoff():
    remote = FakeRemote(); remote.html = lambda _: '<html><body>Too ordinary</body></html>'
    remote.comments = lambda _: (_ for _ in ()).throw(JobError('read_failed'))
    counts, state, _ = run(remote)
    assert counts['failed'] == 1 and state['errors']['task1']['attempts'] == 1
    counts, state, _ = run(remote, state)
    assert counts['pending'] == 1 and not remote.calls


def test_no_credential_read_when_disabled(tmp_path):
    config = tmp_path / 'config.json'; config.write_text('{"enabled":false}'); config.chmod(0o600)
    assert once(config, apply=True) == {'status': 'disabled'}


def test_grant_required_before_token_or_network(tmp_path):
    config = tmp_path / 'config.json'; config.write_text(json.dumps({'enabled': True, 'project_id': PROJECT})); config.chmod(0o600)
    with pytest.raises(JobError, match='credential_grant_required'):
        once(config, apply=True)


def test_unsafe_and_symlink_config(tmp_path):
    config = tmp_path / 'config.json'; config.write_text('{}'); config.chmod(0o644)
    with pytest.raises(JobError): protected_read(config)
    link = tmp_path / 'link'; link.symlink_to(config)
    with pytest.raises(OSError): protected_read(link)


def test_corrupt_or_destination_changed_state_fails_closed():
    state = {'version':VERSION, 'project_id':PROJECT, 'receipts':{}}
    validate_state(state, PROJECT)
    with pytest.raises(JobError): validate_state(state, 'different')
    state['receipts']['../outside'] = {'status':'sending'}
    with pytest.raises(JobError): validate_state(state, PROJECT)


def test_reconciliation_budget_bounds_existing_unknowns():
    remote = FakeRemote([task('task1'),task('task2'),task('task3')])
    state = {'receipts': {t['id']:{'status':'sending','description_hash':digest('intended')} for t in remote.items}}
    counts, _, _ = run(remote, state, max_tasks=1)
    assert counts['failed'] == 1 and counts['pending'] == 2 and not remote.calls


def test_incomplete_task_scan_and_attachment_size_limit():
    def transport(request):
        if request.url.host == 'files.todoist.com':
            return httpx.Response(200, content=b'x' * 2_000_001)
        return httpx.Response(200, json={'results':[], 'next_cursor':'more'})
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        remote = Remote('fake-token', client)
        with pytest.raises(JobError, match='task_scan_limit'): remote.tasks(PROJECT)
        with pytest.raises(JobError, match='source_too_large'): remote.html(comment()['attachment']['file_url'])


def test_token_only_goes_to_api_and_no_redirects():
    calls = []
    def transport(request):
        calls.append(request)
        return httpx.Response(200, text=HTML) if request.url.host == 'files.todoist.com' else httpx.Response(200, json=task())
    with httpx.Client(transport=httpx.MockTransport(transport), follow_redirects=False) as client:
        remote = Remote('fake-token', client)
        remote.task('task1'); remote.html(comment()['attachment']['file_url'])
        assert calls[0].headers['Authorization'] == 'Bearer fake-token'
        assert 'Authorization' not in calls[1].headers
        with pytest.raises(JobError, match='unsafe_attachment'):
            remote.html('https://example.com/file.html')


def test_transport_retries_reads_but_never_writes(monkeypatch):
    monkeypatch.setattr('workbench.newsletter_job.time.sleep', lambda _: None)
    attempts = []
    def transport(request):
        attempts.append(request.method)
        raise httpx.ReadTimeout('private response')
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        remote = Remote('fake-token', client)
        with pytest.raises(JobError, match='read_failed'): remote.task('task1')
        assert len(attempts) == 3
        with pytest.raises(JobError, match='write_unknown'): remote.update('task1', 'full body')
        assert attempts == ['GET', 'GET', 'GET', 'POST']
