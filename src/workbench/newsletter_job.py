"""Opt-in, bounded enrichment of existing Todoist newsletter tasks."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import stat
import time
import uuid

import httpx

from .newsletter_inline import VERSION, ConversionError, convert, source_matches
from .newsletter_download import GRANT, DownloadError, read_html, validate_proof

API = 'https://api.todoist.com/api/v1/'


class JobError(RuntimeError):
    """Fixed codes only; HTTP bodies and task content must not enter logs."""


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


LINK = re.compile(r'\[([^\]\n]+)\]\((https?://[^)\n]+)\)')


def self_links(description: str) -> list[str]:
    def address(value):
        return re.sub(r'^https?://', '', value).rstrip('/')
    return [url for label, url in LINK.findall(description) if address(label) == address(url)]


def description_hash(description: str, urls: list[str]) -> str:
    # Todoist resolves URL-as-label links to page titles asynchronously. Ignore
    # only labels of those exact self-links, never article labels or body text.
    normalized = LINK.sub(lambda match: '[URL](' + match[2] + ')' if match[2] in urls else match[0], description)
    return digest(normalized)


def protected_read(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise JobError('unsafe_private_file')
        with os.fdopen(fd, encoding='utf-8', closefd=False) as stream:
            return stream.read()
    finally:
        os.close(fd)


def private_dir(path: Path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise JobError('unsafe_state_directory')


def validate_state(state: dict, project: str):
    if state.get('version') != VERSION or state.get('project_id') != project or not isinstance(state.get('receipts'), dict):
        raise JobError('state_identity_mismatch')
    if not isinstance(state.get('errors', {}), dict) or len(state['receipts']) > 10000 or len(state.get('errors', {})) > 10000:
        raise JobError('invalid_state')
    for task_id, receipt in state['receipts'].items():
        if not isinstance(task_id, str) or not task_id.isalnum() or not isinstance(receipt, dict):
            raise JobError('invalid_receipt')
        if receipt.get('version') != VERSION or receipt.get('status') not in {'sending', 'verified'}:
            raise JobError('invalid_receipt')
        if not all(isinstance(receipt.get(k), str) and re.fullmatch('[a-f0-9]{64}', receipt[k]) for k in ['source_hash','description_hash']):
            raise JobError('invalid_receipt')
        if not isinstance(receipt.get('self_link_urls'), list) or any(not isinstance(u, str) for u in receipt['self_link_urls']):
            raise JobError('invalid_receipt')
    for task_id, error in state.get('errors', {}).items():
        if not isinstance(task_id, str) or not task_id.isalnum() or not isinstance(error, dict):
            raise JobError('invalid_retry_state')
        if not isinstance(error.get('attempts'), int) or not isinstance(error.get('not_before'), (int, float)):
            raise JobError('invalid_retry_state')


def atomic_json(path: Path, data: dict):
    temp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(data, out, ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        temp.unlink(missing_ok=True)


class Remote:
    def __init__(self, token: str, client: httpx.Client | None = None, *, attachment_auth: bool = False):
        self.token = token
        self.client = client or httpx.Client(timeout=10, trust_env=False, follow_redirects=False)
        self.attachment_auth = attachment_auth

    def request(self, method: str, path: str, **kwargs):
        for attempt in range(3 if method == 'GET' else 1):
            try:
                result = self.client.request(method, API + path, headers={'Authorization': 'Bearer ' + self.token}, follow_redirects=False, **kwargs)
            except httpx.HTTPError:
                if method == 'GET' and attempt < 2:
                    time.sleep(attempt + 1)
                    continue
                raise JobError('read_failed' if method == 'GET' else 'write_unknown') from None
            if method == 'GET' and result.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                time.sleep(attempt + 1)
                continue
            if result.status_code >= 300:
                raise JobError('remote_rejected' if result.status_code < 500 else 'remote_unavailable')
            try:
                return result.json()
            except ValueError:
                raise JobError('invalid_response') from None

    def tasks(self, project_id: str):
        cursor = None
        items = []
        for _ in range(10):
            payload = self.request('GET', 'tasks', params={'project_id': project_id, 'limit': 100, **({'cursor': cursor} if cursor else {})})
            if not isinstance(payload, dict) or not isinstance(payload.get('results'), list):
                raise JobError('invalid_task_page')
            items.extend(payload['results'])
            cursor = payload.get('next_cursor')
            if not cursor:
                return items
        raise JobError('task_scan_limit')

    def task(self, task_id: str):
        return self.request('GET', 'tasks/' + task_id)

    def comments(self, task_id: str):
        payload = self.request('GET', 'comments', params={'task_id': task_id, 'limit': 100})
        if not isinstance(payload, dict) or not isinstance(payload.get('results'), list) or payload.get('next_cursor'):
            raise JobError('comments_incomplete')
        # API v1 uses item_id/file_attachment; connector snapshots use
        # task_id/attachment. Preserve the server's actual task binding.
        return [{**comment, 'task_id': comment.get('task_id', comment.get('item_id')),
                 'attachment': comment.get('attachment', comment.get('file_attachment'))}
                for comment in payload['results'] if not comment.get('is_deleted', False)]

    def html(self, url: str):
        try:
            return read_html(self.client, url, token=self.token if self.attachment_auth else None)
        except DownloadError as error:
            raise JobError(str(error)) from None

    def update(self, task_id: str, description: str):
        return self.request('POST', 'tasks/' + task_id, json={'description': description})


def eligible(task: dict, project: str):
    return (task.get('project_id', task.get('projectId')) == project
            and isinstance(task.get('id'), str) and task['id'].isalnum()
            and not task.get('is_completed', task.get('checked', False))
            and not task.get('description', ''))


def process(remote, config: dict, state: dict, save, *, apply: bool):
    """Journal before writes; reconcile uncertain writes by exact readback only."""
    project = config['project_id']
    receipts = state['receipts']
    errors = state.setdefault('errors', {})
    counts = {'updated': 0, 'unchanged': 0, 'skipped': 0, 'failed': 0, 'pending': 0}
    started = time.monotonic()
    processed = 0
    for task_id, receipt in receipts.items():
        if receipt['status'] == 'sending':
            if processed >= config.get('max_tasks', 25) or time.monotonic() - started >= config.get('max_seconds', 180):
                counts['pending'] += 1
                continue
            processed += 1
            current = remote.task(task_id)
            if (current.get('project_id', current.get('projectId')) == project
                    and description_hash(current.get('description', ''), receipt.get('self_link_urls', [])) == receipt['description_hash']):
                if apply:
                    receipt['status'] = 'verified'
                    save(state)
            else:
                # A lost response must not cause a blind retry or overwrite.
                counts['failed'] += 1
    candidates = remote.tasks(project)
    seen = set()
    for task in candidates:
        task_id = task.get('id')
        if task_id in seen:
            continue
        seen.add(task_id)
        if task_id in receipts:
            counts['unchanged'] += 1
            continue
        if errors.get(task_id, {}).get('not_before', 0) > time.time():
            counts['pending'] += 1
            continue
        if not eligible(task, project):
            counts['skipped'] += 1
            continue
        if processed >= config.get('max_tasks', 25) or time.monotonic() - started >= config.get('max_seconds', 180):
            counts['pending'] += 1
            continue
        processed += 1
        try:
            attachments = [c for c in remote.comments(task_id)
                           if (c.get('attachment', c.get('fileAttachment', {})) or {}).get('file_type', (c.get('attachment', c.get('fileAttachment', {})) or {}).get('fileType')) == 'text/html']
            if len(attachments) != 1:
                counts['skipped'] += 1
                continue
            comment = attachments[0]
            attachment = comment.get('attachment', comment.get('fileAttachment'))
            html = remote.html(attachment.get('file_url', attachment.get('fileUrl')))
            if not source_matches(task, comment, html, project):
                counts['skipped'] += 1
                continue
            converted = convert(html)
            if not apply:
                counts['pending'] += 1
                continue
            if len(receipts) >= 10000:
                raise JobError('receipt_limit')
            # The API has no conditional description update. This narrows, but
            # cannot eliminate, a concurrent edit between read and POST.
            fresh = remote.task(task_id)
            if not eligible(fresh, project) or fresh.get('content') != task.get('content'):
                counts['skipped'] += 1
                continue
            receipt = {'status': 'sending', 'version': VERSION, 'source_comment_id': comment['id'],
                       'source_hash': digest(html), 'self_link_urls': self_links(converted.description),
                       'description_hash': description_hash(converted.description, self_links(converted.description))}
            backup = {'task_before': fresh, 'comment': comment, 'html': html, 'description_after': converted.description}
            save(backup, task_id=task_id)
            receipts[task_id] = receipt
            save(state)
            remote.update(task_id, converted.description)
            saved = remote.task(task_id)
            if description_hash(saved.get('description', ''), receipt['self_link_urls']) != receipt['description_hash']:
                raise JobError('readback_mismatch')
            receipt['status'] = 'verified'
            errors.pop(task_id, None)
            save(state)
            counts['updated'] += 1
        except (JobError, ConversionError, httpx.HTTPError, ValueError, KeyError, TypeError):
            counts['failed'] += 1
            if apply and task_id not in receipts:
                if task_id not in errors and len(errors) >= 10000:
                    raise JobError('retry_state_limit')
                attempts = errors.get(task_id, {}).get('attempts', 0) + 1
                errors[task_id] = {'attempts': min(attempts, 20), 'not_before': time.time() + min(86400, 300 * 2 ** min(attempts, 8))}
                save(state)
    return counts


def once(config_path: Path, *, apply: bool):
    config = json.loads(protected_read(config_path))
    if config.get('enabled') is not True:
        return {'status': 'disabled'}
    if not isinstance(config.get('project_id'), str) or not config['project_id'].isalnum():
        raise JobError('invalid_project')
    if not (1 <= config.get('max_tasks', 25) <= 100 and 10 <= config.get('max_seconds', 180) <= 600):
        raise JobError('invalid_bounds')
    if config.get('credential_grant') != 'read-newsletter-attachments-and-update-existing-descriptions.v1':
        raise JobError('credential_grant_required')
    directory = Path(config['state_dir']).expanduser()
    if not directory.is_absolute():
        raise JobError('absolute_state_path_required')
    private_dir(directory)
    attachment_auth = False
    if config.get('attachment_grant') is not None:
        if config['attachment_grant'] != GRANT:
            raise JobError('invalid_attachment_grant')
        try:
            validate_proof(json.loads(protected_read(directory / 'attachment-proof.json')), config['project_id'])
        except DownloadError:
            raise JobError('live_attachment_proof_required') from None
        attachment_auth = True
    with os.fdopen(os.open(directory / 'lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600), 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        token_path = Path(config['token_file']).expanduser()
        if not token_path.is_absolute():
            raise JobError('absolute_token_path_required')
        token = protected_read(token_path).strip()
        if not token or any(c.isspace() for c in token):
            raise JobError('invalid_token')
        state_file = directory / 'state.json'
        state = json.loads(protected_read(state_file)) if state_file.exists() else {'version': VERSION, 'project_id': config['project_id'], 'receipts': {}}
        validate_state(state, config['project_id'])
        def save(data, *, task_id=None):
            if task_id:
                backup = directory / ('backup-' + task_id + '.json')
                if backup.exists():
                    raise JobError('backup_already_exists')
                atomic_json(backup, data)
            else:
                atomic_json(state_file, data)
        with httpx.Client(timeout=10, trust_env=False, follow_redirects=False) as client:
            counts = process(Remote(token, client, attachment_auth=attachment_auth), config, state, save, apply=apply)
        report = {'status': 'failed' if counts['failed'] else 'success', 'version': VERSION, 'finished_at': time.time(),
                  'name': 'newsletter-inline', 'exit_code': 1 if counts['failed'] else 0,
                  'ended_at': datetime.now(timezone.utc).isoformat(), **counts}
        if apply:
            atomic_json(directory / 'last-run.json', report)
            if config.get('status_file'):
                status_file = Path(config['status_file']).expanduser()
                if not status_file.is_absolute():
                    raise JobError('absolute_status_path_required')
                private_dir(status_file.parent)
                atomic_json(status_file, report)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        result = once(args.config, apply=args.apply)
        print(json.dumps(result))
        return 1 if result['status'] == 'failed' else 0
    except (OSError, ValueError, JobError, KeyError, TypeError):
        print(json.dumps({'status': 'failed', 'error': 'configuration_or_state_or_transport_failure'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
