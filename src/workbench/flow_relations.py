"""Opt-in, minimal FLOW projection and exact action relations in the central API."""
from __future__ import annotations

import hashlib
import json

from .models import now
from .repository import Problem


def _request_hash(kind: str, data: dict) -> str:
    content = json.dumps([kind, data], sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


def _replay(db, operation_id: str, request_hash: str) -> dict | None:
    row = db.execute('SELECT * FROM flow_publication_ops WHERE operation_id=?', (operation_id,)).fetchone()
    if not row:
        return None
    if row['request_hash'] != request_hash:
        raise Problem(409, 'operation_conflict', 'Operation ID was used for different FLOW content')
    return json.loads(row['response_json'])


def _remember(db, operation_id: str, request_hash: str, result: dict) -> None:
    db.execute('INSERT INTO flow_publication_ops VALUES (?,?,?)',
               (operation_id, request_hash, json.dumps(result, sort_keys=True)))


def _view(db, work_item_id: str) -> dict:
    row = db.execute('SELECT * FROM flow_work_items WHERE work_item_id=?', (work_item_id,)).fetchone()
    if not row:
        raise Problem(404, 'not_found', 'FLOW work item projection does not exist')
    item = dict(row)
    item['task_ids'] = json.loads(item['task_ids'])
    links = db.execute('''SELECT l.action_id,l.linked_document_revision,l.linked_action_version,
        l.linked_at,l.linked_by,a.status AS action_status,a.version AS action_version
        FROM flow_action_links l JOIN actions a ON a.id=l.action_id
        WHERE l.work_item_id=? ORDER BY l.linked_at,l.action_id''', (work_item_id,)).fetchall()
    item['actions'] = [{**dict(link),
                        'definition_drift': link['linked_document_revision'] != item['document_revision']}
                       for link in links]
    item['authority'] = 'projection only; action status and source tracker state are separate'
    return item


def get_work_item(repo, work_item_id: str) -> dict:
    with repo.connection() as db:
        return _view(db, work_item_id)


def list_work_items(repo, limit: int, offset: int) -> list[dict]:
    with repo.connection() as db:
        ids = [row['work_item_id'] for row in db.execute(
            'SELECT work_item_id FROM flow_work_items ORDER BY updated_at DESC,work_item_id LIMIT ? OFFSET ?',
            (limit, offset))]
        return [_view(db, identity) for identity in ids]


def publish_work_item(repo, data: dict, principal: str) -> dict:
    data = dict(data)
    operation_id = data.pop('operation_id')
    request_hash = _request_hash('project', data)
    with repo.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        prior = _replay(db, operation_id, request_hash)
        if prior is not None:
            return prior
        work_item_id = data['work_item_id']
        existing = db.execute('SELECT * FROM flow_work_items WHERE work_item_id=?',
                              (work_item_id,)).fetchone()
        expected = data.get('expected_version')
        task_ids = json.dumps(data['task_ids'], ensure_ascii=False, separators=(',', ':'))
        if existing:
            if existing['source_ref'] != data['source_ref']:
                raise Problem(409, 'identity_conflict', 'FLOW source identity cannot change silently')
            if expected != existing['version']:
                raise Problem(409, 'version_conflict', 'Read the current FLOW projection before publishing')
            changed = (existing['document_revision'] != data['document_revision'] or
                       existing['document_hash'] != data['document_hash'] or
                       existing['task_ids'] != task_ids)
            if changed:
                db.execute('''UPDATE flow_work_items SET document_revision=?,document_hash=?,task_ids=?,
                    version=?,updated_at=?,updated_by=? WHERE work_item_id=?''',
                    (data['document_revision'], data['document_hash'], task_ids,
                     existing['version'] + 1, now(), principal, work_item_id))
            status = 'updated' if changed else 'unchanged'
        else:
            if expected is not None:
                raise Problem(409, 'version_conflict', 'FLOW projection does not exist at the expected version')
            if db.execute('SELECT 1 FROM flow_work_items WHERE source_ref=?',
                          (data['source_ref'],)).fetchone():
                raise Problem(409, 'identity_conflict', 'FLOW source already belongs to another work item')
            db.execute('''INSERT INTO flow_work_items
                (work_item_id,source_ref,document_revision,document_hash,task_ids,projection_state,
                 version,updated_at,updated_by) VALUES (?,?,?,?,?,?,?,?,?)''',
                (work_item_id, data['source_ref'], data['document_revision'], data['document_hash'],
                 task_ids, 'proposed', 1, now(), principal))
            status = 'created'
        result = {'status': status, 'projection': _view(db, work_item_id),
                  'publication': 'projection_only', 'source_tracker_status': 'not_changed'}
        _remember(db, operation_id, request_hash, result)
        db.commit()
        return result


def link_work_item_action(repo, work_item_id: str, action_id: str,
                          data: dict, principal: str) -> dict:
    data = dict(data)
    operation_id = data.pop('operation_id')
    request_hash = _request_hash('link', {'work_item_id': work_item_id,
                                          'action_id': action_id, **data})
    with repo.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        prior = _replay(db, operation_id, request_hash)
        if prior is not None:
            return prior
        projection = _view(db, work_item_id)
        action = repo._get(db, 'actions', action_id)
        if projection['version'] != data['work_item_version'] or action['version'] != data['action_version']:
            raise Problem(409, 'version_conflict', 'Read both current records before linking them')
        existing = db.execute('SELECT * FROM flow_action_links WHERE work_item_id=? AND action_id=?',
                              (work_item_id, action_id)).fetchone()
        if existing:
            if existing['linked_document_revision'] != projection['document_revision']:
                raise Problem(409, 'definition_drift', 'Existing action link needs deliberate scope reconciliation')
            status = 'existing'
        else:
            db.execute('''INSERT INTO flow_action_links
                (work_item_id,action_id,linked_document_revision,linked_action_version,linked_at,linked_by)
                VALUES (?,?,?,?,?,?)''',
                (work_item_id, action_id, projection['document_revision'], action['version'],
                 now(), principal))
            status = 'linked'
        result = {'status': status, 'projection': _view(db, work_item_id),
                  'action_status': action['status'], 'action_version': action['version'],
                  'action_changed': False, 'source_tracker_status': 'not_changed'}
        _remember(db, operation_id, request_hash, result)
        db.commit()
        return result
