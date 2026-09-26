"""Deterministic, bounded local FLOW resume and handoff packets."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from urllib.parse import urlsplit

from .flow import show
from .flow_code import workspace
from .flow_history import snapshot
from .flow_tasks import inspect


def _line(value: str, label: str, limit: int = 300) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or '\n' in value or '\r' in value:
        raise ValueError(f'{label} must be concise single-line text (up to {limit} characters)')
    return value.strip()


def _provided(values: list[str] | None, label: str) -> list[str]:
    rows = values or []
    if len(rows) > 12:
        raise ValueError(f'{label} exceeds the 12-entry limit')
    return [_line(value, label) for value in rows]


def _links(values: list[str] | None) -> list[dict[str, str]]:
    links = []
    for value in _provided(values, 'PR link'):
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.port):
            raise ValueError('PR links must be plain HTTPS URLs without credentials, query, or fragment')
        links.append({'url': value, 'verification': 'caller-provided; not fetched'})
    return links


def build_packet(reference: str, *, profile=None, kind: str = 'resume', role: str | None = None,
                 expected_heads: dict[str, str] | None = None, pr_links: list[str] | None = None,
                 scope: str | None = None, tests: list[str] | None = None,
                 findings: list[str] | None = None, limitations: list[str] | None = None,
                 include_paths: bool = False, max_chars: int = 8000) -> dict:
    """Read local state only. Caller-provided evidence is marked, never verified or persisted."""
    if kind not in {'resume', 'handoff'} or not 2000 <= max_chars <= 12000:
        raise ValueError('Use resume/handoff and a packet budget of 2000..12000 characters')
    if role is not None:
        role = _line(role, 'Role', 80)
    if kind == 'handoff' and not role:
        raise ValueError('Handoff needs an explicitly selected role')
    if scope is not None:
        scope = _line(scope, 'Scope', 500)
    expected = expected_heads or {}
    if len(expected) > 12 or any(not re.fullmatch(r'[0-9a-f]{40,64}', head) for head in expected.values()):
        raise ValueError('Expected heads must be a bounded map of exact commit IDs')
    if any(not re.fullmatch(r'[a-z][a-z0-9-]{0,39}', name) for name in expected):
        raise ValueError('Expected head names must be configured binding names')
    test_rows = _provided(tests, 'Test result')
    finding_rows = _provided(findings, 'Finding')
    limitation_rows = _provided(limitations, 'Limitation')
    links = _links(pr_links)

    item = show(reference, profile=profile)
    if item['status'] != 'existing':
        raise ValueError('Select an existing verified local FLOW work item')
    before = snapshot(reference, profile=profile)
    task_view = inspect(reference, 'list', profile=profile)
    next_view = inspect(reference, 'next', profile=profile)
    history_view = inspect(reference, 'history', profile=profile)
    reconciliation = inspect(reference, 'reconcile', profile=profile)
    try:
        code = workspace(reference, profile=profile, create=False)
    except ValueError as exc:
        if str(exc) != 'No explicit repository binding; use work bind before preparation':
            raise
        code = {'repositories': [], 'repository_bindings': 'none'}
    after = snapshot(reference, profile=profile)
    if (before['revision'], before['document_hash']) != (after['revision'], after['document_hash']) or task_view['revision'] != after['revision']:
        raise ValueError('FLOW metadata changed during packet build; inspect it again')

    repositories = []
    for row in code['repositories'][:12]:
        selected = {key: row[key] for key in ('name', 'status', 'branch', 'head', 'recorded_head',
            'diverged_from_record', 'dirty', 'ownership', 'remote_freshness') if key in row}
        if include_paths:
            selected.update({key: row[key] for key in ('repository', 'worktree') if key in row})
        if row.get('status') == 'error':
            selected['probe'] = 'unknown; inspect code workspace locally'
        expected_head = expected.get(row['name'])
        selected['expected_head'] = expected_head
        selected['head_match'] = ('unknown' if expected_head is None or not row.get('head') else
                                  'matches' if expected_head == row['head'] else 'stale')
        repositories.append(selected)
    tasks = []
    truncated_fields = 0
    for row in task_view['tasks'][:30]:
        selected = {key: row[key] for key in ('task_id', 'priority', 'checked_claim')}
        selected['title'] = row['title'][:300]
        truncated_fields += int(len(row['title']) > 300)
        selected['unresolved_dependencies'] = row['unresolved_dependencies'][:12]
        truncated_fields += max(0, len(row['unresolved_dependencies']) - 12)
        for key in ('Details', 'Acceptance', 'Blocked'):
            if key in row['fields']:
                value = row['fields'][key]
                selected[key.lower()] = value[:300]
                truncated_fields += int(len(value) > 300)
        tasks.append(selected)
    suggestion = next_view['suggestion']
    bounded_suggestion = next((row for row in tasks if suggestion and row['task_id'] == suggestion['task_id']), None)
    observed_names = {row['name'] for row in repositories}
    expected_statuses = [row['head_match'] for row in repositories if row['name'] in expected]
    packet = {
        'kind': kind, 'generated_at': datetime.now(timezone.utc).isoformat(),
        'work_item_id': item['work_item_id'], 'source_issue': item['source'],
        'metadata_revision': after['revision'], 'document_hash': after['document_hash'],
        'tasks': tasks, 'next_suggestion': bounded_suggestion,
        'recent_checkpoints': [{key: entry[key] for key in ('checkpoint', 'operation', 'task_id')}
                               for entry in history_view['entries'][:12]],
        'reconciliation': {'dirty_target': reconciliation['dirty_target'],
                           'manual_checked': reconciliation['manual_checked'][:20],
                           'missing_since_checkpoint': reconciliation['missing_since_checkpoint'][:20]},
        'repositories': repositories,
        'selected_role': role, 'selected_scope': scope,
        'pr_links': links, 'tests': [{'result': value, 'verification': 'caller-provided'} for value in test_rows],
        'unresolved_findings': [{'finding': value, 'verification': 'caller-provided'} for value in finding_rows],
        'limitations': [{'limitation': value, 'verification': 'caller-provided'} for value in limitation_rows],
        'review_head_status': ('stale' if 'stale' in expected_statuses
                               else 'matches_selected_heads' if expected and set(expected) <= observed_names and all(status == 'matches' for status in expected_statuses)
                               else 'unknown'),
        'authorization': 'This packet records context only; execute the next step only when the request authorizes it.',
        'delivery': 'not sent', 'agent_session': 'not started',
        'central_action': 'not queried or changed', 'source_tracker_status': 'not queried',
        'publication': 'not attempted by packet',
        'omissions': {'tasks': max(0, len(task_view['tasks']) - 30),
                      'repositories': max(0, len(code['repositories']) - 12),
                      'older_outcomes': max(0, len(history_view['entries']) - 12) or int(history_view['older_history_may_exist']),
                      'older_history_may_exist': history_view['older_history_may_exist'],
                      'manual_claims': max(0, len(reconciliation['manual_checked']) - 20),
                      'missing_tasks': max(0, len(reconciliation['missing_since_checkpoint']) - 20),
                      'task_fields_truncated': truncated_fields, 'paths_withheld': not include_paths,
                      'suggestion_outside_task_limit': suggestion is not None and bounded_suggestion is None,
                      'expected_heads_without_binding': sorted(set(expected) - observed_names)},
    }
    while len(json.dumps(packet, ensure_ascii=False)) > max_chars and packet['tasks']:
        packet['tasks'].pop()
        packet['omissions']['tasks'] += 1
        if packet['next_suggestion'] and packet['next_suggestion'] not in packet['tasks']:
            packet['next_suggestion'] = None
            packet['omissions']['suggestion_outside_task_limit'] = True
    while len(json.dumps(packet, ensure_ascii=False)) > max_chars and packet['recent_checkpoints']:
        packet['recent_checkpoints'].pop()
        packet['omissions']['older_outcomes'] += 1
    if len(json.dumps(packet, ensure_ascii=False)) > max_chars:
        raise ValueError('Packet exceeds the selected budget; reduce supplied evidence or use a larger bounded budget')
    return packet
