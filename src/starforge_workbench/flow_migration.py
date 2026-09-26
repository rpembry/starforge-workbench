"""Preview-first adoption of one explicitly selected legacy Markdown checklist."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re

from .flow_history import snapshot
from .flow_tasks import mutate

CHECKBOX = re.compile(r'^- \[([ xX])\] (.+)$')
FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})')


def _operation_id(item_id: str, source_hash: str, line: int) -> str:
    return 'migrate-' + hashlib.sha256((item_id + source_hash + str(line)).encode()).hexdigest()[:32]


def _read(path: str | Path) -> bytes:
    selected = Path(path).expanduser()
    if not selected.is_absolute() or selected.is_symlink() or not selected.is_file():
        raise ValueError('Select an absolute nonsymlink Markdown file')
    if selected.stat().st_uid != os.getuid() or selected.stat().st_size > 1024 * 1024:
        raise ValueError('Selected checklist must be owned by you and at most 1 MiB')
    if selected.suffix.lower() != '.md':
        raise ValueError('Select a Markdown file; filename case does not define its format')
    return selected.read_bytes()


def preview(reference: str, source_file: str | Path, *, profile=None) -> dict:
    content = _read(source_file)
    text = content.decode('utf-8')
    source_hash = hashlib.sha256(content).hexdigest()
    target = snapshot(reference, profile=profile)
    entries = []
    unsupported = []
    fence = None
    for line_no, line in enumerate(text.splitlines(), 1):
        marker = FENCE.match(line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if fence:
            continue
        match = CHECKBOX.fullmatch(line)
        if match:
            title = match.group(2).strip()
            if not title or len(title) > 500:
                unsupported.append({'line': line_no, 'reason': 'empty or oversized task'})
                continue
            checked = match.group(1).lower() == 'x'
            entries.append({'line': line_no, 'title': title,
                            'classification': 'checked_claim_requires_review' if checked else 'proposed_active',
                            'proposed_task_id': None if checked else
                            'task-' + hashlib.sha256((target['work_item_id'] +
                                _operation_id(target['work_item_id'], source_hash, line_no)).encode()).hexdigest()[:16]})
        elif re.match(r'^\s+- \[[ xX]\]', line):
            unsupported.append({'line': line_no, 'reason': 'nested checklist stays in the original'})
    if len(entries) > 50 or len(unsupported) > 50:
        raise ValueError('Checklist preview exceeds 50 entries or unsupported lines; select a smaller file')
    return {'source_hash': source_hash,
            'source_file': str(Path(source_file).expanduser()),
            'work_item_id': target['work_item_id'], 'target_revision': target['revision'],
            'target_hash': target['document_hash'], 'entries': entries,
            'unsupported': unsupported,
            'note': 'Original remains untouched. Checked claims are not imported as completed work; continuation text and nested checklists need manual review.'}


def apply(reference: str, source_file: str | Path, *, expected_source_hash: str,
          expected_revision: str, expected_target_hash: str, profile=None) -> dict:
    """Deliberate adoption of only active top-level rows after exact preview review."""
    current = preview(reference, source_file, profile=profile)
    if current['source_hash'] != expected_source_hash or current['target_revision'] != expected_revision or current['target_hash'] != expected_target_hash:
        raise ValueError('Source or target changed since preview; inspect again before adoption')
    if current['unsupported']:
        raise ValueError('Unsupported nested checklist content needs manual review before adoption')
    imported = []
    for entry in current['entries']:
        if entry['classification'] != 'proposed_active':
            continue
        operation_id = _operation_id(current['work_item_id'], current['source_hash'], entry['line'])
        result = mutate(reference, 'add', title=entry['title'], profile=profile, operation_id=operation_id)
        imported.append({'source_line': entry['line'], 'task_id': result['task_id'],
                         'checkpoint': result['checkpoint']})
    return {'status': 'imported_active_only', 'work_item_id': current['work_item_id'],
            'imported': imported, 'checked_claims_skipped': sum(
                row['classification'] == 'checked_claim_requires_review' for row in current['entries']),
            'source_file_unchanged': True}
