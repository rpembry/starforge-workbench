"""A bounded, span-preserving adapter for the documented FLOW tasks.md subset."""
from __future__ import annotations

from dataclasses import dataclass
import re

HEADING = re.compile(r'^## (P[0-3])\s*$')
CHECKBOX = re.compile(r'^- \[([ xX])\] (.+)$')
FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})')
FIELD = re.compile(r'^  - \*\*([^*]+)\*\*: (.*)$')
ID = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')


@dataclass(frozen=True)
class Task:
    start: int
    end: int
    section: str
    title: str
    checked: bool
    task_id: str | None
    fields: dict[str, str]
    block: str


@dataclass(frozen=True)
class Document:
    text: str
    newline: str
    tasks: list[Task]
    sections: dict[str, int]

    def selected(self, task_id: str) -> Task:
        matches = [task for task in self.tasks if task.task_id == task_id]
        if len(matches) != 1:
            raise ValueError('Task ID is missing or ambiguous; reconcile or explicitly assign an ID')
        return matches[0]


def parse(text: str, *, validate_dependencies: bool = True) -> Document:
    if len(text.encode('utf-8')) > 1024 * 1024:
        raise ValueError('FLOW task document exceeds the 1 MiB inspection limit')
    if not text.startswith('# Tasks\n') and not text.startswith('# Tasks\r\n'):
        raise ValueError('FLOW document must begin with # Tasks')
    newline = '\r\n' if '\r\n' in text else '\n'
    if '\n' in text.replace('\r\n', '') and newline == '\r\n':
        raise ValueError('Mixed line endings need manual reconciliation')
    lines = text.splitlines(keepends=True)
    positions: list[int] = []
    offset = 0
    for line in lines:
        positions.append(offset)
        offset += len(line)
    sections: dict[str, int] = {}
    starts: list[tuple[int, str, str, bool]] = []
    current: str | None = None
    fence: str | None = None
    for index, line in enumerate(lines):
        body = line.rstrip('\r\n')
        marker = FENCE.match(body)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if fence:
            continue
        heading = HEADING.fullmatch(body)
        if heading:
            current = heading.group(1)
            if current in sections:
                raise ValueError(f'Duplicate FLOW priority section: {current}')
            sections[current] = index
            continue
        if body.startswith('## '):
            current = None
        checkbox = CHECKBOX.fullmatch(body)
        if checkbox:
            if current is None:
                raise ValueError('Top-level checkbox is outside a P0-P3 section')
            starts.append((index, current, checkbox.group(2), checkbox.group(1).lower() == 'x'))
    if fence:
        raise ValueError('Unclosed Markdown fence needs manual reconciliation')
    tasks: list[Task] = []
    for line_index, section, title, checked in starts:
        end_line = line_index + 1
        while end_line < len(lines):
            body = lines[end_line].rstrip('\r\n')
            if body and not body[0].isspace():
                break
            end_line += 1
        start = positions[line_index]
        end = positions[end_line] if end_line < len(lines) else len(text)
        block = text[start:end]
        fields = {}
        for line in block.splitlines()[1:]:
            match = FIELD.fullmatch(line)
            if match:
                if match.group(1) in fields and match.group(1) in {'ID', 'Blocked by'}:
                    raise ValueError(f'Duplicate task field: {match.group(1)}')
                fields[match.group(1)] = match.group(2)
        task_id = fields.get('ID')
        if task_id is not None and not ID.fullmatch(task_id):
            raise ValueError('Malformed FLOW task ID')
        tasks.append(Task(start, end, section, title, checked, task_id, fields, block))
    if len(tasks) > 200:
        raise ValueError('FLOW task document exceeds the 200-task inspection limit')
    by_id = {task.task_id: task for task in tasks if task.task_id}
    if len(by_id) != len([task for task in tasks if task.task_id]):
        raise ValueError('Duplicate FLOW task IDs need reconciliation')
    if validate_dependencies:
        edges = {task.task_id: [part.strip() for part in task.fields.get('Blocked by', '').split(',') if part.strip()]
                 for task in tasks if task.task_id}
        visiting: set[str] = set()
        visited: set[str] = set()
        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ValueError('FLOW dependency cycle needs reconciliation')
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in edges.get(task_id, []):
                if not ID.fullmatch(dependency):
                    raise ValueError('Malformed FLOW dependency ID')
                if dependency in edges:
                    visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)
        for task_id in edges:
            visit(task_id)
    return Document(text, newline, tasks, sections)


def replace_task(document: Document, task: Task, replacement: str) -> str:
    return document.text[:task.start] + replacement + document.text[task.end:]


def add_field(task: Task, name: str, value: str, newline: str) -> str:
    if '\n' in value or '\r' in value or not value.strip():
        raise ValueError('FLOW field must be concise single-line text')
    lines = task.block.splitlines(keepends=True)
    for index, line in enumerate(lines):
        match = FIELD.fullmatch(line.rstrip('\r\n'))
        if match and match.group(1) == name:
            lines[index] = f'  - **{name}**: {value}{newline}'
            return ''.join(lines)
    insert = 1
    while insert < len(lines) and lines[insert].startswith('  - **'):
        insert += 1
    lines.insert(insert, f'  - **{name}**: {value}{newline}')
    return ''.join(lines)
