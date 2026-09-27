"""Deterministic local FLOW task operations; no tracker or agent side effects."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from uuid import uuid4

from .flow import load_profile
from .flow_history import _item, _run, mutate_document, operation_state, revision, snapshot
from .flow_markdown import ID, add_field, parse, remove_field, replace_task


def _read(reference: str, profile=None):
    state = snapshot(reference, profile=profile)
    return state, parse(state['document'])


def _history(reference: str, profile=None, limit: int = 200) -> list[dict]:
    path, config = load_profile(profile)
    if revision(Path(config['root'])) == 'UNBORN':
        return []
    item, _, relative = _item(Path(config['root']), path, config, reference)
    output = _run(Path(config['root']), 'log', f'--max-count={limit}', '--format=%H%x00%B%x00',
                  '--', relative).stdout
    chunks = output.split('\x00')
    records = []
    for index in range(0, len(chunks) - 1, 2):
        commit = chunks[index].strip()
        message = chunks[index + 1].strip()
        if not re.fullmatch(r'[0-9a-f]{40,64}', commit):
            continue
        operation = re.match(r'^FLOW (\S+)', message)
        item_match = re.search(r'^FLOW-Work-Item: (.+)$', message, re.MULTILINE)
        task_match = re.search(r'^FLOW-Task: (.+)$', message, re.MULTILINE)
        if operation and item_match and item_match.group(1) == item['id']:
            records.append({'checkpoint': commit, 'operation': operation.group(1),
                            'work_item_id': item['id'], 'task_id': task_match.group(1) if task_match else None,
                            'message': message[:1000]})
    return records


def _completed(reference: str, profile=None) -> set[str]:
    latest: dict[str, str] = {}
    for entry in _history(reference, profile):
        if entry['task_id'] and entry['operation'] in {'complete', 'cancel', 'supersede', 'reopen'}:
            latest.setdefault(entry['task_id'], entry['operation'])
    return {task_id for task_id, operation in latest.items() if operation == 'complete'}


def inspect(reference: str, command: str, *, task_id: str | None = None, profile=None) -> dict:
    state, document = _read(reference, profile)
    if command == 'history':
        entries = _history(reference, profile)
        return {'revision': state['revision'], 'entries': [entry for entry in entries if not task_id or entry['task_id'] == task_id],
                'limit': 200, 'older_history_may_exist': len(entries) == 200}
    if command == 'reconcile':
        path, config = load_profile(profile)
        _, target, relative = _item(Path(config['root']), path, config, reference)
        committed = _run(Path(config['root']), 'show', f'HEAD:{relative}', check=False)
        previous = parse(committed.stdout).tasks if committed.returncode == 0 else []
        current_ids = {task.task_id for task in document.tasks if task.task_id}
        return {'revision': state['revision'], 'document_hash': state['document_hash'],
                'manual_checked': [task.task_id or task.title for task in document.tasks if task.checked],
                'missing_since_checkpoint': [task.task_id for task in previous if task.task_id and task.task_id not in current_ids],
                'dirty_target': committed.returncode == 0 and committed.stdout != target.read_text(encoding='utf-8'),
                'next_action': 'Review the exact document and history; choose an explicit repair. No outcome is inferred.'}
    entries = []
    completed = _completed(reference, profile) - {task.task_id for task in document.tasks if task.task_id}
    children: dict[str, list[str]] = {}
    for task in document.tasks:
        parent = task.fields.get('Parent task')
        if parent and task.task_id:
            children.setdefault(parent, []).append(task.task_id)
    for task in document.tasks:
        missing = [dep.strip() for dep in task.fields.get('Blocked by', '').split(',') if dep.strip() and dep.strip() not in completed]
        entries.append({'task_id': task.task_id, 'title': task.title, 'priority': task.section,
                        'checked_claim': task.checked, 'fields': task.fields,
                        'parent_task_id': task.fields.get('Parent task'),
                        'child_task_ids': children.get(task.task_id, []),
                        'unresolved_dependencies': missing})
    if command == 'show':
        active = next((task for task in document.tasks if task.task_id == task_id), None)
        if active:
            return {'status': 'active', 'revision': state['revision'],
                    'document_hash': state['document_hash'],
                    'task': next(entry for entry in entries if entry['task_id'] == active.task_id),
                    'raw': active.block}
        latest = next((entry for entry in _history(reference, profile)
                       if entry['task_id'] == task_id and entry['operation'] in
                       {'complete', 'cancel', 'supersede', 'reopen'}), None)
        if latest and latest['operation'] in {'complete', 'cancel', 'supersede'}:
            return {'status': latest['operation'], 'revision': state['revision'],
                    'task_id': task_id, 'checkpoint': latest['checkpoint'],
                    'message': latest['message']}
        raise ValueError('Task is not active and has no terminal outcome; inspect history or reconcile manual changes')
    if command == 'next':
        chosen = next((entry for entry in sorted(entries, key=lambda row: row['priority'])
                       if entry['task_id'] and not entry['checked_claim'] and
                       not entry['unresolved_dependencies'] and not entry['fields'].get('Blocked')), None)
        return {'revision': state['revision'], 'suggestion': chosen,
                'rationale': 'Highest-priority active task with an ID and no unresolved blocker; recommendation only' if chosen else 'No eligible task; inspect blockers and unclassified claims'}
    return {'revision': state['revision'], 'document_hash': state['document_hash'], 'tasks': entries}


def _append(document, section: str, block: str) -> str:
    if section not in {'P0', 'P1', 'P2', 'P3'}:
        raise ValueError('Priority must be P0, P1, P2, or P3')
    text = document.text
    if section not in document.sections:
        return text.rstrip('\r\n') + document.newline * 2 + f'## {section}' + document.newline * 2 + block
    later = [task for task in document.tasks if task.section == section]
    if later:
        position = later[-1].end
        return text[:position] + block + text[position:]
    lines = text.splitlines(keepends=True)
    insertion = sum(len(line) for line in lines[:document.sections[section] + 1])
    return text[:insertion] + document.newline + block + text[insertion:]


def _single_line(value: str | None, name: str) -> None:
    if value is not None and ('\r' in value or '\n' in value or not value.strip()):
        raise ValueError(f'{name} must be nonempty single-line text')


def _relation_field(task, block: str, name: str, value: str | None, newline: str) -> str:
    changed = type(task)(task.start, task.end, task.section, task.title,
                         task.checked, task.task_id, task.fields, block)
    return add_field(changed, name, value, newline) if value else remove_field(changed, name)


def _validate_relations(document, task_id: str, blocked_by: list[str] | None,
                        parent_task_id: str | None, completed: set[str]) -> None:
    active = {task.task_id for task in document.tasks if task.task_id}
    if blocked_by is not None:
        if (not isinstance(blocked_by, list) or len(blocked_by) > 32 or
                any(not isinstance(dep, str) or len(dep) > 120 or
                    not ID.fullmatch(dep) for dep in blocked_by)):
            raise ValueError('Blocked-by IDs must be a list of up to 32 valid task IDs')
        if len(set(blocked_by)) != len(blocked_by):
            raise ValueError('Duplicate FLOW dependency ID')
        if task_id in blocked_by:
            raise ValueError('A FLOW task cannot depend on itself')
        if any(dep not in active | completed for dep in blocked_by):
            raise ValueError('FLOW dependency must identify a task in this work item with a known active or completed outcome')
    if parent_task_id is not None:
        if (not isinstance(parent_task_id, str) or len(parent_task_id) > 120 or
                not ID.fullmatch(parent_task_id) or parent_task_id == task_id or
                parent_task_id not in active):
            raise ValueError('FLOW parent must identify another active task in this work item')


def mutate(reference: str, command: str, *, profile=None, task_id: str | None = None,
           title: str | None = None, details: str | None = None, acceptance: str | None = None,
           blocked_by: list[str] | None = None, parent_task_id: str | None = None,
           clear_parent: bool = False,
           reason: str | None = None, evidence: str = '', priority: str = 'P2',
           from_revision: str | None = None, actor: str = 'local operator',
           operation_id: str | None = None, expected_revision: str | None = None,
           expected_hash: str | None = None, preview: bool = False,
           checkpoint_pending: bool = False) -> dict:
    operation_id = operation_id or 'operation-' + uuid4().hex
    client_args = [command, task_id, title, details, acceptance,
                   reason, evidence, priority, from_revision, actor]
    # Preserve replay hashes for operations created before task relations existed.
    if blocked_by is not None or parent_task_id is not None or clear_parent:
        client_args.extend([blocked_by, parent_task_id, clear_parent])
    client_hash = hashlib.sha256(json.dumps(client_args, ensure_ascii=False).encode()).hexdigest()
    prior = operation_state(reference, operation_id, profile=profile)
    if prior:
        if prior['operation'] != command or prior.get('client_hash') != client_hash:
            raise ValueError('Operation ID was previously used for different command arguments')
        if prior['status'] == 'committed':
            return prior['result']
        current = snapshot(reference, profile=profile)
        if current['document_hash'] == prior['after_hash']:
            return mutate_document(reference, profile=profile, operation_id=operation_id,
                expected_revision=prior['expected_revision'], expected_hash=prior['before_hash'],
                proposed=current['document'], operation=command, actor=actor,
                task_id=prior['task_id'], evidence=evidence or reason or '',
                checkpoint_pending=checkpoint_pending, client_hash=client_hash)
    state, document = _read(reference, profile)
    if command not in {'add', 'update'} and (blocked_by is not None or parent_task_id is not None or clear_parent):
        raise ValueError('Task relationships can only be changed through add or update')
    if clear_parent and parent_task_id is not None:
        raise ValueError('Set or clear the parent task, not both')
    if command == 'add' and clear_parent:
        raise ValueError('A new task has no parent to clear')
    for name, value in [('title', title), ('details', details), ('acceptance', acceptance),
                        ('reason', reason), ('evidence', evidence)]:
        if value:
            _single_line(value, name)
    if expected_revision is not None and expected_revision != state['revision']:
        raise ValueError('Stale expected revision; inspect the work item again')
    if expected_hash is not None and expected_hash != state['document_hash']:
        raise ValueError('Stale expected document hash; inspect the work item again')
    if command in {'add', 'update', 'block', 'complete', 'cancel', 'supersede', 'reopen', 'assign-id'}:
        if any(task.checked for task in document.tasks):
            raise ValueError('Manual [x] claims need explicit reconciliation before mutation')
    generated_id = None
    if command == 'add':
        if not title or not title.strip():
            raise ValueError('Add needs a single-line title')
        generated_id = 'task-' + hashlib.sha256((state['work_item_id'] + operation_id).encode()).hexdigest()[:16]
        _validate_relations(document, generated_id, blocked_by, parent_task_id,
                            _completed(reference, profile) if blocked_by else set())
        newline = document.newline
        block = f'- [ ] {title}{newline}  - **ID**: {generated_id}{newline}'
        if parent_task_id:
            block += f'  - **Parent task**: {parent_task_id}{newline}'
        if blocked_by:
            block += f'  - **Blocked by**: {", ".join(blocked_by)}{newline}'
        if details:
            block += f'  - **Details**: {details}{newline}'
        if acceptance:
            block += f'  - **Acceptance**: {acceptance}{newline}'
        block += newline
        proposed = _append(document, priority, block)
    elif command == 'reopen':
        if not task_id or not from_revision or not re.fullmatch(r'[0-9a-f]{40,64}', from_revision):
            raise ValueError('Reopen needs task ID and explicit historical commit ID')
        if any(task.task_id == task_id for task in document.tasks):
            raise ValueError('Task is already active')
        terminal = [entry for entry in _history(reference, profile) if entry['task_id'] == task_id and
                    entry['operation'] in {'complete', 'cancel', 'supersede'}]
        if not terminal:
            raise ValueError('No recorded terminal outcome for this ID; reconcile history')
        path, config = load_profile(profile)
        _, _, relative = _item(Path(config['root']), path, config, reference)
        historical = _run(Path(config['root']), 'show', f'{from_revision}:{relative}')
        old = parse(historical.stdout).selected(task_id)
        if old.checked:
            raise ValueError('Historical checked claim is not a valid active definition')
        proposed = _append(document, old.section, old.block)
    else:
        if command == 'assign-id':
            if not title or (not preview and (not expected_revision or not expected_hash)):
                raise ValueError('Assign-ID requires exact title and a prior preview revision/hash')
            matches = [task for task in document.tasks if task.title == title and task.task_id is None]
            if len(matches) != 1:
                raise ValueError('ID-less title is missing or ambiguous')
            selected = matches[0]
            generated_id = 'task-' + hashlib.sha256((state['work_item_id'] + operation_id).encode()).hexdigest()[:16]
            proposed = replace_task(document, selected, add_field(selected, 'ID', generated_id, document.newline))
        else:
            selected = document.selected(task_id or '')
            if command == 'update':
                _validate_relations(document, selected.task_id, blocked_by, parent_task_id,
                                    _completed(reference, profile) if blocked_by else set())
                block = selected.block
                if title is not None:
                    if not title.strip() or '\n' in title or '\r' in title:
                        raise ValueError('Title must be single-line')
                    block = re.sub(r'^- \[ \] .+(\r?\n)', lambda m: f'- [ ] {title}{m.group(1)}', block, count=1)
                changed = selected
                for name, value in [('Details', details), ('Acceptance', acceptance)]:
                    if value is not None:
                        changed = type(selected)(selected.start, selected.end, selected.section, selected.title,
                                                 selected.checked, selected.task_id, selected.fields, block)
                        block = add_field(changed, name, value, document.newline)
                if parent_task_id is not None or clear_parent:
                    block = _relation_field(selected, block, 'Parent task',
                                            None if clear_parent else parent_task_id, document.newline)
                if blocked_by is not None:
                    block = _relation_field(selected, block, 'Blocked by',
                                            ', '.join(blocked_by), document.newline)
                proposed = replace_task(document, selected, block)
            elif command == 'block':
                if not reason:
                    raise ValueError('Block needs a reason')
                proposed = replace_task(document, selected, add_field(selected, 'Blocked', reason, document.newline))
            elif command in {'complete', 'cancel', 'supersede'}:
                if command == 'complete' and not evidence:
                    raise ValueError('Complete needs concise evidence')
                if command in {'cancel', 'supersede'} and not reason:
                    raise ValueError('Cancel/supersede needs a reason')
                proposed = replace_task(document, selected, '')
                if not evidence:
                    evidence = reason or ''
            else:
                raise ValueError('Unsupported task operation')
    parse(proposed)
    if preview:
        return {'status': 'preview', 'operation': command, 'task_id': generated_id or task_id,
                'operation_id': operation_id, 'revision': state['revision'],
                'document_hash': state['document_hash'], 'proposed': proposed}
    result = mutate_document(reference, profile=profile, operation_id=operation_id,
        expected_revision=state['revision'], expected_hash=state['document_hash'], proposed=proposed,
        operation=command, actor=actor, task_id=generated_id or task_id, evidence=evidence,
        checkpoint_pending=checkpoint_pending, client_hash=client_hash)
    return result


def _render_text(command: str, result: dict) -> str:
    revision = result.get('revision') or result.get('after_revision')
    lines = [f'Revision: {revision}'] if revision else []
    if command == 'list':
        by_parent: dict[str | None, list[dict]] = {}
        for task in result['tasks']:
            by_parent.setdefault(task['parent_task_id'], []).append(task)
        def render(task: dict, depth: int) -> None:
            indent = '  ' * depth
            lines.append(f"{indent}{task['priority']} [{task['task_id'] or 'no ID'}] {task['title']}")
            if task['unresolved_dependencies']:
                lines.append(indent + '  Waiting for: ' + ', '.join(task['unresolved_dependencies']))
            for child in by_parent.get(task['task_id'], []):
                render(child, depth + 1)
        for task in by_parent.get(None, []):
            render(task, 0)
        if not result['tasks']:
            lines.append('No active tasks.')
    elif command == 'show':
        if result['status'] == 'active':
            task = result['task']
            lines.append(f"{task['priority']} [{task['task_id']}] {task['title']}")
            for name, value in task['fields'].items():
                lines.append(f'  {name}: {value}')
        else:
            lines.extend([f"Task {result['task_id']}: {result['status']}",
                          f"Checkpoint: {result['checkpoint']}"])
    elif command == 'next':
        task = result['suggestion']
        lines.append(f"Suggested: {task['priority']} [{task['task_id']}] {task['title']}" if task
                     else 'No eligible task.')
        lines.append(result['rationale'])
    elif command == 'history':
        for entry in result['entries']:
            lines.append(f"{entry['checkpoint'][:12]} {entry['operation']} {entry['task_id'] or '-'}")
        if not result['entries']:
            lines.append('No recorded FLOW outcomes in the bounded history.')
    elif command == 'reconcile':
        lines.extend([f"Checked claims: {', '.join(result['manual_checked']) or 'none'}",
                      f"Missing since checkpoint: {', '.join(result['missing_since_checkpoint']) or 'none'}",
                      f"Dirty target: {'yes' if result['dirty_target'] else 'no'}",
                      result['next_action']])
    elif result.get('status') == 'preview':
        lines.extend([f"Preview: {result['operation']} {result['task_id'] or '-'}",
                      f"Operation ID: {result['operation_id']}",
                      f"Document hash: {result['document_hash']}",
                      'Proposed TASKS.md:', result['proposed']])
    else:
        lines.extend([f"{result['status']}: {result['operation']} {result['task_id'] or '-'}",
                      f"Checkpoint: {result['checkpoint']}",
                      f"Publication: {result['publication']}"])
    return '\n'.join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='ai-workbench tasks')
    parser.add_argument('--profile', type=Path)
    parser.add_argument('--format', choices=['json', 'text'], default='json')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ['list', 'next', 'reconcile', 'history', 'show', 'add', 'update', 'block',
                 'complete', 'cancel', 'supersede', 'reopen', 'assign-id']:
        item = sub.add_parser(name)
        item.add_argument('reference')
        if name in {'show', 'update', 'block', 'complete', 'cancel', 'supersede', 'reopen'}:
            item.add_argument('task_id')
        if name in {'add', 'assign-id'}:
            item.add_argument('title')
        if name == 'history':
            item.add_argument('task_id', nargs='?')
        if name in {'add', 'update', 'block', 'complete', 'cancel', 'supersede', 'reopen', 'assign-id'}:
            item.add_argument('--preview', action='store_true')
            item.add_argument('--expected-revision')
            item.add_argument('--expected-hash')
            item.add_argument('--operation-id')
            item.add_argument('--actor', default='local operator')
            item.add_argument('--checkpoint-pending', action='store_true')
        if name == 'add':
            item.add_argument('--priority', default='P2')
        if name in {'update', 'add'}:
            item.add_argument('--details')
            item.add_argument('--acceptance')
            item.add_argument('--blocked-by', action='append')
            item.add_argument('--parent-task-id')
        if name == 'update':
            item.add_argument('--title')
            item.add_argument('--clear-blocked-by', action='store_true')
            item.add_argument('--clear-parent', action='store_true')
        if name in {'block', 'cancel', 'supersede'}:
            item.add_argument('--reason', required=True)
        if name == 'complete':
            item.add_argument('--evidence', required=True)
        if name == 'reopen':
            item.add_argument('--from-revision', required=True)
        if name == 'reconcile':
            item.add_argument('--dry-run', action='store_true', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command in {'list', 'show', 'next', 'history', 'reconcile'}:
            result = inspect(args.reference, args.command, task_id=getattr(args, 'task_id', None), profile=args.profile)
        else:
            values = vars(args)
            options = {key: values[key] for key in ('task_id', 'title', 'details', 'acceptance',
                'blocked_by', 'parent_task_id', 'clear_parent', 'reason',
                'evidence', 'priority', 'from_revision', 'actor', 'operation_id', 'expected_revision',
                'expected_hash', 'preview', 'checkpoint_pending') if key in values}
            if values.get('clear_blocked_by'):
                if values.get('blocked_by'):
                    raise ValueError('Set or clear dependencies, not both')
                options['blocked_by'] = []
            result = mutate(args.reference, args.command, profile=args.profile, **options)
        if args.format == 'text':
            print(_render_text(args.command, result))
        else:
            print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        parser.exit(2, f'FLOW: {exc}\n')
