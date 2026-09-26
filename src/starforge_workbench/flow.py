"""Private, host-local FLOW work-item registry. No tracker or provider access."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unicodedata
from urllib.parse import quote, urlsplit
from uuid import uuid4


DEFAULT_PROFILE = Path.home() / '.config/starforge-ai-workbench/flow-profile.json'
SEGMENT = re.compile(r'^[^/\\\x00-\x1f.][^/\\\x00-\x1f]*$')
JIRA_KEY = re.compile(r'^[A-Z][A-Z0-9]+-[1-9][0-9]*$')
GITHUB_PATH = re.compile(r'^/([^/]+)/([^/]+)/(issues|pull)/([1-9][0-9]*)/?$')
NETWORK_FS = {'nfs', 'nfs4', 'cifs', 'smb2', 'fuse.sshfs', '9p'}


def _private(path: Path, *, directory: bool) -> None:
    if path.is_symlink() or not path.exists() or path.stat().st_uid != os.getuid():
        raise ValueError(f'FLOW path must exist, be owned by you, and not be a symlink: {path}')
    mode = path.stat().st_mode
    if mode & 0o077 or (directory and not path.is_dir()) or (not directory and not path.is_file()):
        raise ValueError(f'FLOW path must be private and have the expected type: {path}')


def _safe_components(root: Path, relative: str) -> Path:
    if not relative or Path(relative).is_absolute() or any(part in {'.', '..', ''} for part in relative.split('/')):
        raise ValueError('Unsafe FLOW workspace path')
    target = root
    for part in relative.split('/'):
        target = target / part
        if target.is_symlink():
            raise ValueError('FLOW workspace path contains a symlink')
    if not target.resolve(strict=False).is_relative_to(root.resolve(strict=True)):
        raise ValueError('FLOW workspace escapes profile root')
    return target


def _atomic_json(path: Path, value: object) -> None:
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextlib.contextmanager
def _writer_lock(profile: Path):
    lock = profile.with_suffix('.lock')
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(fd).st_uid != os.getuid() or os.fstat(fd).st_mode & 0o077:
            raise ValueError('FLOW lock is not private')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except BlockingIOError:
        raise ValueError('FLOW profile has another writer; retry') from None
    finally:
        os.close(fd)


def _profile_path(path: str | Path | None) -> Path:
    candidate = Path(path or os.environ.get('WB_FLOW_PROFILE', DEFAULT_PROFILE)).expanduser()
    if not candidate.is_absolute() or candidate.is_symlink():
        raise ValueError('FLOW profile must be an absolute nonsymlink path')
    return candidate


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True,
                            timeout=10, check=False, env={**os.environ, 'GIT_CONFIG_NOSYSTEM': '1'})
    if result.returncode:
        raise ValueError('FLOW metadata Git repository is unavailable or invalid')
    return result.stdout.strip()


def _inside_checkout(root: Path) -> bool:
    for parent in (root, *root.parents):
        marker = parent / '.git'
        if marker.is_symlink():
            return True  # Do not trust a linked marker, including a broken link.
        if not marker.exists():
            continue
        try:
            result = subprocess.run(['git', '-C', str(parent), 'rev-parse', '--show-toplevel'],
                                    capture_output=True, text=True, timeout=10, check=False,
                                    env={**os.environ, 'GIT_CONFIG_NOSYSTEM': '1'})
        except (OSError, subprocess.TimeoutExpired):
            return True  # Probe uncertainty must not weaken the checkout guard.
        if result.returncode == 0 and Path(result.stdout.strip()).resolve() == parent.resolve():
            return True
    return False


def init_profile(root: str | Path, *, profile: str | Path | None = None,
                 github_hosts: list[str] | None = None, github_repositories: list[str] | None = None,
                 jira_sites: dict[str, str] | None = None, dry_run: bool = False) -> dict:
    path = _profile_path(profile)
    root = Path(root).expanduser()
    if not root.is_absolute() or root.is_symlink():
        raise ValueError('Select an absolute nonsymlink FLOW root')
    if path.exists():
        _, existing = load_profile(path)
        if existing['root'] == str(root):
            return {'status': 'existing', 'root': str(root), 'profile': str(path)}
        raise ValueError('FLOW profile already selects a different root')
    if root.exists() and any(root.iterdir()):
        raise ValueError('FLOW root is nonempty; no existing content is adopted')
    if _inside_checkout(root):
        raise ValueError('FLOW root cannot be inside a source or existing Git checkout')
    hosts = github_hosts or ['github.com']
    repos = github_repositories or []
    sites = jira_sites or {}
    for host in hosts:
        if not re.fullmatch(r'[a-z0-9.-]+', host) or host.startswith('.'):
            raise ValueError('Invalid configured GitHub host')
    for repository in repos:
        _parse_url(repository, hosts, sites, repository_only=True)
    for alias, base in sites.items():
        parsed = urlsplit(base)
        if (not re.fullmatch(r'[a-z][a-z0-9-]*', alias) or parsed.scheme != 'https'
                or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.port):
            raise ValueError('Invalid Jira site mapping')
    proposed = {'status': 'preview' if dry_run else 'created', 'root': str(root), 'profile': str(path)}
    if dry_run:
        return proposed
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _private(path.parent, directory=True)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _private(root, directory=True)
    filesystem = subprocess.run(['stat', '-f', '-c', '%T', str(root)], capture_output=True, text=True, timeout=5, check=True).stdout.strip()
    if filesystem in NETWORK_FS:
        raise ValueError('Shared/network filesystem is unsupported for the single-writer FLOW profile')
    subprocess.run(['git', '-c', 'init.templateDir=/dev/null', 'init', '-q', '-b', 'flow-history', str(root)],
                   check=True, timeout=10)
    (root / '.gitignore').write_text('*\n!/.gitignore\n!/work/\n!/work/**/\n!/work/**/TASKS.md\n!/work/**/CONTEXT.md\n!/work/**/SPEC.md\n!/work/**/PLAN.md\n', encoding='utf-8')
    (root / '.gitignore').chmod(0o600)
    _atomic_json(path, {'version': 1, 'root': str(root), 'github_hosts': hosts,
                        'github_repositories': repos, 'jira_sites': sites})
    _atomic_json(path.with_suffix('.registry.json'), {'version': 1, 'items': []})
    return proposed


def load_profile(profile: str | Path | None = None) -> tuple[Path, dict]:
    path = _profile_path(profile)
    _private(path, directory=False)
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('version') != 1:
        raise ValueError('Unsupported FLOW profile version')
    root = Path(data['root'])
    _private(root, directory=True)
    if Path(_git(root, 'rev-parse', '--show-toplevel')) != root.resolve():
        raise ValueError('FLOW root is not the selected metadata Git repository')
    return path, data


def _registry(path: Path) -> dict:
    registry_path = path.with_suffix('.registry.json')
    _private(registry_path, directory=False)
    data = json.loads(registry_path.read_text(encoding='utf-8'))
    if data.get('version') != 1 or not isinstance(data.get('items'), list):
        raise ValueError('Invalid FLOW registry')
    return data


def _segment(value: str) -> str:
    value = unicodedata.normalize('NFC', value).casefold()
    if not SEGMENT.fullmatch(value) or value in {'.', '..'} or value.endswith('.'):
        raise ValueError('Invalid source identity segment')
    return quote(value, safe='-_').replace('%', '~')


def _parse_url(reference: str, hosts: list[str], sites: dict[str, str], repository_only=False) -> dict:
    parsed = urlsplit(reference)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.port:
        raise ValueError('Use an HTTPS canonical source URL without credentials, query, or fragment')
    host = parsed.hostname.lower()
    if host in hosts:
        match = re.fullmatch(r'/([^/]+)/([^/]+)/?', parsed.path) if repository_only else GITHUB_PATH.fullmatch(parsed.path)
        if not match:
            raise ValueError('Expected a configured GitHub repository or issue URL')
        owner, repo = _segment(match.group(1)), _segment(match.group(2))
        if repository_only:
            return {'repository': f'https://{host}/{owner}/{repo}'}
        kind, number = match.group(3), match.group(4)
        return {'canonical': f'https://{host}/{owner}/{repo}/{kind}/{number}',
                'relative': f'work/github/{host}/{owner}/{repo}/{"issue" if kind == "issues" else "pull"}-{number}',
                'kind': kind}
    if repository_only:
        raise ValueError('Repository host is not configured')
    for alias, base in sites.items():
        site = urlsplit(base)
        if site.hostname == host and parsed.path.startswith(site.path.rstrip('/') + '/browse/'):
            key = parsed.path.rsplit('/', 1)[-1].upper()
            if JIRA_KEY.fullmatch(key):
                return {'canonical': base.rstrip('/') + '/browse/' + key,
                        'relative': f'work/jira/{alias}/{key}', 'kind': 'jira'}
    raise ValueError('Reference is outside configured source hosts/sites')


def resolve(reference: str, data: dict) -> dict:
    hosts, sites = data['github_hosts'], data['jira_sites']
    if reference.startswith('https://'):
        return _parse_url(reference, hosts, sites)
    if reference.startswith('github:'):
        return _parse_url('https://' + reference.removeprefix('github:').replace('#', '/issues/'), hosts, sites)
    if reference.startswith('jira:'):
        alias, sep, key = reference.removeprefix('jira:').partition(':')
        if not sep or alias not in sites:
            raise ValueError('Use jira:<configured-site>:<KEY>')
        return _parse_url(sites[alias].rstrip('/') + '/browse/' + key, hosts, sites)
    if JIRA_KEY.fullmatch(reference.upper()):
        if len(sites) != 1:
            raise ValueError('Jira key is ambiguous; use jira:<configured-site>:<KEY>')
        alias = next(iter(sites))
        return resolve(f'jira:{alias}:{reference}', data)
    repo, sep, number = reference.partition('#')
    if sep and re.fullmatch(r'[1-9][0-9]*', number):
        matches = [item for item in data['github_repositories'] if urlsplit(item).path.strip('/').split('/')[-1].casefold() == repo.casefold()]
        if len(matches) != 1:
            raise ValueError('Repository shorthand is ambiguous or unconfigured; use a qualified GitHub URL')
        return _parse_url(matches[0].rstrip('/') + '/issues/' + number, hosts, sites)
    raise ValueError('Use a canonical URL, github:<host>/<owner>/<repo>#N, jira:<site>:<KEY>, or unambiguous shorthand')


def _find(registry: dict, canonical: str) -> dict | None:
    return next((item for item in registry['items'] if canonical == item['source'] or canonical in item['aliases']), None)


def _display(item: dict) -> dict:
    return {'status': 'existing', 'work_item_id': item['id'], 'source': item['source'], 'aliases': item['aliases']}


def show(reference: str, *, profile=None) -> dict:
    path, data = load_profile(profile)
    canonical = resolve(reference, data)['canonical']
    item = _find(_registry(path), canonical)
    if item is None:
        return {'status': 'unverified', 'source': canonical}
    root = Path(data['root'])
    target = _safe_components(root, item['relative'])
    document = target / 'TASKS.md'
    if not document.is_file() or document.is_symlink():
        return {'status': 'error', 'reason': 'registered TASKS.md is missing or unsafe', **_display(item)}
    content = document.read_text(encoding='utf-8')
    if f'<!-- FLOW work-item: {item["id"]} -->' not in content or f'<!-- Source: {item["source"]} -->' not in content:
        return {'status': 'error', 'reason': 'registered TASKS.md identity differs', **_display(item)}
    return _display(item)


def list_items(*, profile=None) -> dict:
    path, _ = load_profile(profile)
    return {'items': [_display(item) for item in _registry(path)['items']]}


def open_item(reference: str, *, profile=None, dry_run=False, link_to: str | None = None) -> dict:
    path, data = load_profile(profile)
    source = resolve(reference, data)
    root = Path(data['root'])
    if dry_run:
        prior = _find(_registry(path), source['canonical'])
        if prior:
            return _display(prior)
        if link_to:
            linked = _find(_registry(path), resolve(link_to, data)['canonical'])
            if not linked:
                raise ValueError('Link target must be an existing registered work item')
            return {'status': 'preview', 'source': source['canonical'], 'link_to': linked['id']}
        return {'status': 'preview', 'source': source['canonical'], 'relative': source['relative']}
    with _writer_lock(path):
        registry = _registry(path)
        prior = _find(registry, source['canonical'])
        if prior:
            return show(reference, profile=profile)
        if link_to:
            existing = _find(registry, resolve(link_to, data)['canonical'])
            if not existing:
                raise ValueError('Link target must be an existing registered work item')
            existing['aliases'].append(source['canonical'])
            _atomic_json(path.with_suffix('.registry.json'), registry)
            return _display(existing)
        target = _safe_components(root, source['relative'])
        if target.exists():
            document = target / 'TASKS.md'
            if document.is_file() and not document.is_symlink():
                content = document.read_text(encoding='utf-8')
                match = re.search(r'^<!-- FLOW work-item: (wi-[0-9a-f]{32}) -->$', content, re.MULTILINE)
                if match and f'<!-- Source: {source["canonical"]} -->' in content:
                    item = {'id': match.group(1), 'source': source['canonical'], 'aliases': [], 'relative': source['relative']}
                    registry['items'].append(item)
                    _atomic_json(path.with_suffix('.registry.json'), registry)
                    return {'status': 'recovered', **{k: v for k, v in _display(item).items() if k != 'status'}}
            raise ValueError('FLOW target exists but is unmanaged; refusing adoption')
        target.mkdir(parents=True, mode=0o700)
        for parent in (target, *target.parents):
            if parent == root.parent:
                break
            if parent.is_symlink():
                raise ValueError('FLOW target path changed to a symlink')
            if parent != root:
                parent.chmod(0o700)
        identity = 'wi-' + uuid4().hex
        document = target / 'TASKS.md'
        document.write_text(f'# Tasks\n\n<!-- FLOW work-item: {identity} -->\n<!-- Source: {source["canonical"]} -->\n', encoding='utf-8')
        document.chmod(0o600)
        item = {'id': identity, 'source': source['canonical'], 'aliases': [], 'relative': source['relative']}
        registry['items'].append(item)
        _atomic_json(path.with_suffix('.registry.json'), registry)
        return {'status': 'created', **{k: v for k, v in _display(item).items() if k != 'status'}}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='ai-workbench work')
    parser.add_argument('--profile', type=Path)
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init')
    init.add_argument('--root', type=Path, required=True)
    init.add_argument('--github-host', action='append', dest='github_hosts')
    init.add_argument('--github-repo', action='append', dest='github_repositories')
    init.add_argument('--jira-site', action='append', default=[])
    init.add_argument('--dry-run', action='store_true')
    open_parser = sub.add_parser('open')
    open_parser.add_argument('reference')
    open_parser.add_argument('--link-to')
    open_parser.add_argument('--dry-run', action='store_true')
    show_parser = sub.add_parser('show'); show_parser.add_argument('reference')
    sub.add_parser('list')
    bind_parser = sub.add_parser('bind')
    bind_parser.add_argument('reference')
    bind_parser.add_argument('--name', required=True)
    bind_parser.add_argument('--repository', type=Path, required=True)
    bind_parser.add_argument('--worktree-root', type=Path, required=True)
    bind_parser.add_argument('--base-ref', required=True)
    bind_parser.add_argument('--branch')
    bind_parser.add_argument('--remote')
    bind_parser.add_argument('--allow-remote-read', action='store_true')
    for command in ('prepare', 'resume'):
        sub.add_parser(command).add_argument('reference')
    adopt_parser = sub.add_parser('adopt')
    adopt_parser.add_argument('reference')
    adopt_parser.add_argument('--name', required=True)
    adopt_parser.add_argument('--worktree', type=Path, required=True)
    packet_parser = sub.add_parser('packet')
    packet_parser.add_argument('reference')
    packet_parser.add_argument('--kind', choices=['resume', 'handoff'], default='resume')
    packet_parser.add_argument('--role')
    packet_parser.add_argument('--expected-head', action='append', default=[])
    packet_parser.add_argument('--pr-link', action='append', default=[])
    packet_parser.add_argument('--scope')
    packet_parser.add_argument('--test', action='append', default=[])
    packet_parser.add_argument('--finding', action='append', default=[])
    packet_parser.add_argument('--limitation', action='append', default=[])
    packet_parser.add_argument('--include-paths', action='store_true')
    packet_parser.add_argument('--max-chars', type=int, default=8000)
    args = parser.parse_args(argv)
    try:
        if args.command == 'init':
            sites = dict(site.split('=', 1) for site in args.jira_site)
            result = init_profile(args.root, profile=args.profile, github_hosts=args.github_hosts,
                                  github_repositories=args.github_repositories, jira_sites=sites, dry_run=args.dry_run)
        elif args.command == 'open':
            result = open_item(args.reference, profile=args.profile, dry_run=args.dry_run, link_to=args.link_to)
        elif args.command == 'show':
            result = show(args.reference, profile=args.profile)
        elif args.command == 'bind':
            from .flow_code import bind
            result = bind(args.reference, name=args.name, repository=args.repository,
                          worktree_root=args.worktree_root, base_ref=args.base_ref,
                          branch=args.branch, remote=args.remote,
                          allow_remote_read=args.allow_remote_read, profile=args.profile)
        elif args.command in {'prepare', 'resume'}:
            from .flow_code import workspace
            result = workspace(args.reference, profile=args.profile, create=args.command == 'prepare')
        elif args.command == 'adopt':
            from .flow_code import adopt
            result = adopt(args.reference, name=args.name, worktree=args.worktree, profile=args.profile)
        elif args.command == 'packet':
            from .flow_packets import build_packet
            pairs = [item.split('=', 1) for item in args.expected_head]
            if any(len(pair) != 2 for pair in pairs) or len({pair[0] for pair in pairs}) != len(pairs):
                raise ValueError('Use each --expected-head NAME=SHA once')
            expected = dict(pairs)
            result = build_packet(args.reference, profile=args.profile, kind=args.kind, role=args.role,
                                  expected_heads=expected, pr_links=args.pr_link, scope=args.scope,
                                  tests=args.test, findings=args.finding, limitations=args.limitation,
                                  include_paths=args.include_paths, max_chars=args.max_chars)
        else:
            result = list_items(profile=args.profile)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        parser.exit(2, f'FLOW: {exc}\n')
