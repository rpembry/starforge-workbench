"""Explicit, private source-issue snapshots and copyable reviewed updates.

A caller obtains structured provider data through its existing authenticated tool.
No provider credentials, raw descriptions, or tracker writes are stored here.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.parse import urlsplit

from .flow import _atomic_json, _find, _private, _registry, _writer_lock, load_profile, resolve
from .flow_tasks import inspect


class SourceError(ValueError):
    pass


def normalize_github(issue: dict, repository: dict) -> dict:
    """Adapt selected GitHub issue/repository API records; discard body/comments."""
    if not isinstance(issue.get('id'), int) or not isinstance(repository.get('id'), int):
        raise SourceError('GitHub immutable issue and repository IDs are required')
    return {'canonical_url': issue['html_url'], 'provider': 'github',
            'provider_site': urlsplit(issue['html_url']).hostname,
            'immutable_id': f'repository-id:{repository["id"]}/issue-id:{issue["id"]}',
            'title': issue['title'], 'status': issue['state'], 'updated_at': issue['updated_at']}


def normalize_jira(issue: dict, site_url: str) -> dict:
    """Adapt a selected Jira issue API record; discard description/comments."""
    if not str(issue.get('id', '')).isdigit():
        raise SourceError('Jira immutable issue ID is required')
    site = site_url.rstrip('/')
    parsed = urlsplit(site)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SourceError('Jira site must be a plain HTTPS origin')
    fields = issue['fields']
    return {'canonical_url': site + '/browse/' + issue['key'], 'provider': 'jira',
            'provider_site': parsed.hostname, 'immutable_id': 'issue-id:' + issue['id'],
            'title': fields['summary'], 'status': fields['status']['name'],
            'updated_at': fields['updated']}


def _source_path(profile: Path) -> Path:
    return profile.with_suffix('.sources.json')


def _load(profile: Path) -> dict:
    file = _source_path(profile)
    if not file.exists():
        return {'version': 1, 'items': {}}
    _private(file, directory=False)
    state = json.loads(file.read_text(encoding='utf-8'))
    if state.get('version') != 1 or not isinstance(state.get('items'), dict):
        raise SourceError('Invalid FLOW source cache')
    return state


def _bounded(value: str, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(c in value for c in '\r\n\x00'):
        raise SourceError(f'{label} must be concise single-line text')
    return value.strip()


def _identity(reference: str, profile):
    path, config = load_profile(profile)
    canonical = resolve(reference, config)['canonical']
    item = _find(_registry(path), canonical)
    if not item:
        raise SourceError('Open the selected source work item first')
    return path, item


def record(reference: str, snapshot: dict, *, profile=None) -> dict:
    """Record only a caller-selected, structured provider observation."""
    path, item = _identity(reference, profile)
    url = _bounded(snapshot['canonical_url'], 'Canonical URL', 500)
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.port:
        raise SourceError('Source must be a plain HTTPS URL')
    provider = snapshot['provider']
    if provider not in {'github', 'jira'}:
        raise SourceError('Unsupported source provider')
    immutable_id = _bounded(snapshot['immutable_id'], 'Immutable provider ID', 120)
    provider_site = _bounded(snapshot['provider_site'], 'Provider site', 200)
    title = _bounded(snapshot['title'], 'Title', 300)
    source_status = _bounded(snapshot['status'], 'Source status', 80)
    updated_at = datetime.fromisoformat(snapshot['updated_at'].replace('Z', '+00:00'))
    if updated_at.tzinfo is None:
        raise SourceError('Provider update time must include timezone')
    # A move is verified by immutable provider identity, then deliberately linked
    # through `work open --link-to`; this operation never edits the registry.
    if url != item['source'] and url not in item['aliases']:
        raise SourceError('Canonical URL differs; verify identity and add an explicit alias first')
    key = f'{provider}:{provider_site}:{immutable_id}'
    observed = {'identity': key, 'canonical_url': url, 'provider': provider,
                'provider_site': provider_site, 'title': title, 'status': source_status,
                'source_updated_at': updated_at.isoformat(),
                'fetched_at': datetime.now(timezone.utc).isoformat(),
                'verification': 'caller-selected structured provider result'}
    with _writer_lock(path):
        state = _load(path)
        if any(other != item['id'] and value['identity'] == key
               for other, value in state['items'].items()):
            raise SourceError('Immutable provider identity belongs to another work item')
        previous = state['items'].get(item['id'])
        if previous and previous['identity'] != key:
            raise SourceError('Provider identity changed; reconcile the selected work item')
        if previous and all(previous[field] == observed[field] for field in (
                'identity', 'canonical_url', 'title', 'status', 'source_updated_at')):
            return previous
        state['items'][item['id']] = observed
        _atomic_json(_source_path(path), state)
    return observed


def status(reference: str, *, profile=None, error: str | None = None) -> dict:
    path, item = _identity(reference, profile)
    cached = _load(path)['items'].get(item['id'])
    if error not in {None, 'not_found', 'forbidden', 'rate_limited', 'unavailable'}:
        raise SourceError('Unknown provider read result')
    return {'work_item_id': item['id'], 'source': cached,
            'freshness': 'cached; verify against provider' if cached else 'unverified',
            'read_error': error, 'source_issue_changed': False}


def preview_requirements(reference: str, requirements: list[str], *, profile=None) -> dict:
    """An untrusted, bounded preview; never mutates TASKS.md or executes instructions."""
    _, item = _identity(reference, profile)
    if len(requirements) > 20:
        raise SourceError('Too many selected requirements')
    rows = [_bounded(value, 'Requirement', 300) for value in requirements]
    current = inspect(reference, 'list', profile=profile)
    return {'work_item_id': item['id'], 'proposed_requirements': rows,
            'existing_task_ids': [task['task_id'] for task in current['tasks']],
            'status': 'preview_only', 'authorization': 'Review and add tasks explicitly through FLOW'}


def draft_update(reference: str, *, profile=None, outcomes: list[str], tests: list[str],
                 commits: list[str], prs: list[str], limitations: list[str],
                 review: str = 'unknown', merged: str = 'unknown', deployed: str = 'unknown') -> dict:
    """Copyable text only; no tracker mutation or inferred merge/deploy state."""
    path, item = _identity(reference, profile)
    cached = _load(path)['items'].get(item['id'])
    if not cached:
        raise SourceError('Record an exact source identity before drafting a tracker update')
    from re import fullmatch, search, IGNORECASE
    if any(not fullmatch(r'[a-f0-9]{40,64}', head) for head in commits):
        raise SourceError('Commit evidence needs exact hashes')
    for url in prs:
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.port:
            raise SourceError('PR links need plain HTTPS URLs')
    if any(value not in {'unknown', 'pending', 'complete'} for value in (review, merged, deployed)):
        raise SourceError('Review, merge, and deploy states must be explicitly selected')
    def lines(values, label):
        if len(values) > 12:
            raise SourceError(f'Too many {label}')
        rows = [_bounded(v, label, 250) for v in values]
        if any(search(r'(?:^|\s)/(?:home|Users|workspace|tmp|etc)/|\b(?:password|token|secret)\s*[:=]',
                      value, IGNORECASE) for value in rows):
            raise SourceError('Draft contains a local path or credential-like text; redact it first')
        return rows
    parts = [f'Source: {cached["canonical_url"]}',
             'Local FLOW status: caller-selected outcomes; source status unchanged.',
             'Implementation: ' + ('; '.join(lines(outcomes, 'outcomes')) or 'not reported'),
             'Tests: ' + ('; '.join(lines(tests, 'tests')) or 'not reported'),
             'Exact commits: ' + (', '.join(commits) or 'none supplied'),
             'PRs: ' + (', '.join(prs) or 'none supplied'),
             f'Review: {review}; merged: {merged}; deployed: {deployed}',
             'Remaining/limitations: ' + ('; '.join(lines(limitations, 'limitations')) or 'not reported')]
    return {'target': cached['canonical_url'], 'text': '\n'.join(parts),
            'status': 'draft_only', 'tracker_write': False}
