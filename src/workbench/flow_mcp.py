"""Bounded MCP adapters over the local FLOW domain operations."""
from __future__ import annotations

import difflib
import hashlib
import re
from pathlib import Path
from typing import Literal
from uuid import uuid4

from starforge_workbench.flow import list_items, load_profile, open_item, show
from starforge_workbench.flow_code import workspace
from starforge_workbench.flow_history import operation_state, snapshot
from starforge_workbench.flow_tasks import inspect, mutate


TaskRead = Literal['list', 'show', 'next', 'history', 'reconcile']
TaskEdit = Literal['add', 'update', 'block', 'complete', 'cancel', 'supersede', 'reopen', 'assign-id']


def _bounded(value, *, depth=0):
    """Limit model-facing text and collections without changing stored documents."""
    if depth > 8:
        return '[omitted: nesting limit]'
    if isinstance(value, str):
        return value if len(value) <= 1000 else value[:1000] + '… [truncated]'
    if isinstance(value, list):
        result = [_bounded(item, depth=depth + 1) for item in value[:50]]
        if len(value) > 50:
            result.append({'omitted_items': len(value) - 50})
        return result
    if isinstance(value, dict):
        items = [(key, item) for key, item in value.items() if key != 'raw']
        result = {key: _bounded(item, depth=depth + 1) for key, item in items[:50]}
        if len(items) > 50:
            result['omitted_fields'] = len(items) - 50
        return result
    return value


def _private_profile(profile: str | Path) -> Path:
    path = Path(profile).expanduser()
    if not path.is_absolute() or path.is_symlink():
        raise ValueError('FLOW MCP profile must be an absolute nonsymlink path')
    load_profile(path)
    return path


def register_flow_tools(server, *, profile: str | Path, allow_write: bool = False,
                        allow_prepare: bool = False, disclose_paths: bool = False) -> None:
    """Register only enabled tools; caller cannot select a different profile/root."""
    selected = Path(profile)
    previews: dict[str, dict] = {}

    def current_profile() -> Path:
        return _private_profile(selected)

    def redact_workspace(result: dict) -> dict:
        if not disclose_paths:
            for row in result['repositories']:
                for key in ('repository', 'worktree'):
                    row.pop(key, None)
                if row.get('status') == 'error':
                    row['reason'] = 'Local code-workspace probe failed; inspect with the local CLI'
            if result.get('local_context', {}).get('tasks') == 'unknown':
                result['local_context']['reason'] = 'Local task summary unavailable; inspect with the local CLI'
        return _bounded(result)

    @server.tool(name='flow_items', structured_output=True)
    def flow_items(limit: int = 20, offset: int = 0) -> dict[str, object]:
        """List a bounded page of work items from the configured private FLOW profile; no document text."""
        if not 1 <= limit <= 50 or offset < 0 or offset > 10000:
            raise ValueError('Use limit 1..50 and offset 0..10000')
        items = list_items(profile=current_profile())['items']
        return {'items': items[offset:offset + limit], 'total': len(items), 'offset': offset, 'limit': limit}

    @server.tool(name='flow_item_show', structured_output=True)
    def flow_item_show(reference: str) -> dict[str, object]:
        """Resolve one work item in the configured profile; an opaque ID grants no cross-profile access."""
        return show(reference, profile=current_profile())

    @server.tool(name='flow_task_read', structured_output=True)
    def flow_task_read(reference: str, view: TaskRead = 'list', task_id: str | None = None) -> dict[str, object]:
        """Read bounded local tasks/history or ask what remains; next only suggests and never starts work."""
        if view == 'show' and not task_id:
            raise ValueError('Task show requires a task ID')
        return _bounded(inspect(reference, view, task_id=task_id, profile=current_profile()))

    @server.tool(name='flow_operation_status', structured_output=True)
    def flow_operation_status(reference: str, operation_id: str) -> dict[str, object]:
        """Reconcile a possibly interrupted local edit by its operation ID before retrying it."""
        if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', operation_id) or len(operation_id) > 120:
            raise ValueError('Use a bounded stable kebab-case operation ID')
        entry = operation_state(reference, operation_id, profile=current_profile())
        if not entry:
            return {'status': 'absent', 'operation_id': operation_id}
        return _bounded({'status': entry['status'], 'operation_id': operation_id,
                         'operation': entry['operation'], 'work_item_id': entry['work_item_id'],
                         'result': entry.get('result')})

    @server.tool(name='flow_resume_context', structured_output=True)
    def flow_resume_context(reference: str) -> dict[str, object]:
        """Read bounded local task and code-workspace state for a handoff; does not create a worktree or agent session."""
        selected_profile = current_profile()
        try:
            result = workspace(reference, profile=selected_profile, create=False)
        except ValueError as exc:
            if str(exc) != 'No explicit repository binding; use work bind before preparation':
                raise
            item = show(reference, profile=selected_profile)
            result = {'work_item_id': item['work_item_id'], 'repositories': [],
                      'repository_bindings': 'none',
                      'local_context': {'tasks': inspect(reference, 'list', profile=selected_profile)['tasks'],
                                        'next_suggestion': inspect(reference, 'next', profile=selected_profile)['suggestion']},
                      'agent_session': 'not started'}
        return redact_workspace(result)

    if not allow_write:
        return

    @server.tool(name='flow_item_open', structured_output=True)
    def flow_item_open(reference: str, dry_run: bool = True) -> dict[str, object]:
        """Preview or deliberately create one configured-source local work item; no tracker action is accepted or published."""
        if len(reference) > 1000:
            raise ValueError('FLOW source reference is too large')
        return open_item(reference, profile=current_profile(), dry_run=dry_run)

    @server.tool(name='flow_task_preview', structured_output=True)
    def flow_task_preview(reference: str, command: TaskEdit, operation_id: str,
                          expected_revision: str, expected_hash: str,
                          task_id: str | None = None, title: str | None = None,
                          details: str | None = None, acceptance: str | None = None,
                          reason: str | None = None, evidence: str = '', priority: Literal['P0', 'P1', 'P2', 'P3'] = 'P2',
                          from_revision: str | None = None) -> dict[str, object]:
        """Preview an exact authorized TASKS.md edit; this token is bound to the target, content, operation ID and expected revision/hash."""
        if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', operation_id) or len(operation_id) > 120:
            raise ValueError('Use a bounded stable kebab-case operation ID')
        for value in (reference, task_id, title, details, acceptance, reason, evidence, from_revision):
            if value is not None and len(value) > 1000:
                raise ValueError('FLOW MCP text is too large; use the local CLI for deliberate larger edits')
        if len(previews) >= 100:
            raise ValueError('Too many pending previews; restart this MCP server after reconciliation')
        previous = operation_state(reference, operation_id, profile=current_profile())
        if previous:
            raise ValueError('Operation ID already has recovery state; inspect flow_operation_status')
        request = dict(reference=reference, command=command, operation_id=operation_id,
                       expected_revision=expected_revision, expected_hash=expected_hash,
                       task_id=task_id, title=title, details=details, acceptance=acceptance,
                       reason=reason, evidence=evidence, priority=priority,
                       from_revision=from_revision, actor='MCP caller (authorization not independently verified)')
        result = mutate(reference, command, profile=current_profile(), preview=True,
                        **{key: value for key, value in request.items() if key not in {'reference', 'command'}})
        state = snapshot(reference, profile=current_profile())
        if state['revision'] != result['revision'] or state['document_hash'] != result['document_hash']:
            raise ValueError('FLOW document changed during preview; inspect it again')
        changed = ''.join(difflib.unified_diff(
            state['document'].splitlines(keepends=True), result['proposed'].splitlines(keepends=True),
            fromfile='TASKS.md before', tofile='TASKS.md proposed', n=2))
        if len(changed) > 12000:
            raise ValueError('Document change exceeds MCP preview limit; inspect it locally')
        proposed_hash = hashlib.sha256(result['proposed'].encode('utf-8')).hexdigest()
        token = uuid4().hex
        previews[token] = {'request': request, 'proposed_hash': proposed_hash}
        return {key: value for key, value in result.items() if key != 'proposed'} | {
            'change_preview': changed, 'proposed_hash': proposed_hash, 'preview_token': token}

    @server.tool(name='flow_task_apply', structured_output=True)
    def flow_task_apply(preview_token: str) -> dict[str, object]:
        """Apply only the exact previewed local edit after an authorized request; no server action or tracker status changes."""
        saved = previews.get(preview_token)
        if saved is None:
            raise ValueError('Unknown or expired FLOW preview; inspect current state before retrying')
        request = saved['request']
        reference, command = request['reference'], request['command']
        if operation_state(reference, request['operation_id'], profile=current_profile()) is None:
            check = mutate(reference, command, profile=current_profile(), preview=True,
                           **{key: value for key, value in request.items() if key not in {'reference', 'command'}})
            if hashlib.sha256(check['proposed'].encode('utf-8')).hexdigest() != saved['proposed_hash']:
                raise ValueError('FLOW preview content changed; inspect it again')
        return mutate(reference, command, profile=current_profile(),
                      **{key: value for key, value in request.items() if key not in {'reference', 'command'}})

    if allow_prepare:
        @server.tool(name='flow_prepare_workspace', structured_output=True)
        def flow_prepare_workspace(reference: str) -> dict[str, object]:
            """Explicitly prepare only pre-bound local code worktrees; may perform an authorized read-only clone, never starts an agent or pushes Git."""
            result = workspace(reference, profile=current_profile(), create=True)
            return redact_workspace(result)
