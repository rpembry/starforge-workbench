"""Explicit, collision-safe local installation of versioned Workbench skills."""
from __future__ import annotations

import argparse
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import tomllib


SKILLS = ('aiw-flow-capture', 'aiw-flow-resume', 'aiw-flow-handoff', 'aiw-flow-closeout')
RECEIPT = '.aiw-install.json'


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True,
                            timeout=10, check=False)
    if result.returncode:
        raise ValueError('Skill source must be a checked-out Git revision')
    return result.stdout.strip()


def _source(source: str | Path | None, name: str) -> tuple[Path, str, dict[str, str]]:
    if source is None:
        raise ValueError('Select a pinned local --source checkout for preview/install/update')
    root = Path(source).expanduser()
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ValueError('Skill source must be an absolute nonsymlink checkout')
    commit = _git(root, 'rev-parse', '--verify', 'HEAD')
    if Path(_git(root, 'rev-parse', '--show-toplevel')) != root.resolve():
        raise ValueError('Skill source must select the exact Git checkout root')
    if _git(root, 'status', '--porcelain', '--', f'skills/{name}'):
        raise ValueError('Skill source has uncommitted changes; select a committed revision')
    path = root / 'skills' / name
    if not path.is_dir() or path.is_symlink() or not (path / 'SKILL.md').is_file():
        raise ValueError('Selected skill is missing from the pinned source')
    if (path / RECEIPT).exists():
        raise ValueError('Source skill contains installer state; remove it before publication')
    return path, commit, _hashes(path)


def _hashes(root: Path) -> dict[str, str]:
    files = {}
    size = 0
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('Symlinked skill files are not installed')
        relative = path.relative_to(root).as_posix()
        if relative == RECEIPT and path.is_file():
            continue
        if any(part.startswith('.') for part in Path(relative).parts):
            raise ValueError('Hidden files and directories are not installed as skills')
        if path.is_file():
            size += path.stat().st_size
            if path.stat().st_size > 1024 * 1024 or size > 5 * 1024 * 1024:
                raise ValueError('Skill files exceed the local installation size limit')
            files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif not path.is_dir():
            raise ValueError('Skill source contains an unsupported file type')
    if 'SKILL.md' not in files or len(files) > 50:
        raise ValueError('Skill must have SKILL.md and at most 50 files')
    return files


def _target(name: str, *, scope: str, home: Path, repo: Path | None) -> Path:
    if name not in SKILLS:
        raise ValueError('Select a known Workbench skill name')
    if scope == 'user':
        base = home / '.agents' / 'skills'
    elif scope == 'repo':
        if repo is None or not repo.is_absolute() or repo.is_symlink() or not repo.is_dir():
            raise ValueError('Repository scope needs an absolute nonsymlink --repo')
        if Path(_git(repo, 'rev-parse', '--show-toplevel')) != repo.resolve():
            raise ValueError('Repository scope must select its exact Git root')
        base = repo / '.agents' / 'skills'
    else:
        raise ValueError('Scope must be user or repo')
    for candidate in (base.parent, base, base / name):
        if candidate.is_symlink():
            raise ValueError('Skill destination contains a symlink')
    return base / name


def _receipt(target: Path) -> dict | None:
    path = target / RECEIPT
    if not path.is_file() or path.is_symlink():
        return None
    data = json.loads(path.read_text(encoding='utf-8'))
    if (data.get('version') != 1 or data.get('name') != target.name or
            not re.fullmatch(r'[0-9a-f]{40,64}', data.get('source_commit', '')) or
            not isinstance(data.get('files'), dict)):
        raise ValueError('Managed skill receipt is invalid; inspect it manually')
    return data


def _status(target: Path) -> tuple[str, dict | None]:
    for suffix in ('aiw-updating', 'aiw-removing'):
        backup = target.with_name(f'.{target.name}.{suffix}')
        if backup.exists() or backup.is_symlink():
            return 'interrupted', None
    if not target.exists() and not target.is_symlink():
        return 'absent', None
    if target.is_symlink() or not target.is_dir():
        return 'collision', None
    receipt = _receipt(target)
    if receipt is None:
        return 'unmanaged', None
    return ('managed' if _hashes(target) == receipt['files'] else 'modified'), receipt


def _write_copy(source: Path, parent: Path, name: str, commit: str, files: dict[str, str]) -> Path:
    temporary = Path(tempfile.mkdtemp(prefix=f'.{name}.aiw-new-', dir=parent))
    try:
        for relative in files:
            destination = temporary / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / relative, destination)
        if _hashes(temporary) != files:
            raise ValueError('Skill source changed during copy; retry from a stable checkout')
        receipt = {'version': 1, 'name': name, 'source_commit': commit, 'files': files}
        installed = temporary / RECEIPT
        installed.write_text(json.dumps(receipt, sort_keys=True, indent=2) + '\n', encoding='utf-8')
        installed.chmod(0o600)
        return temporary
    except BaseException:
        shutil.rmtree(temporary)
        raise


def manage(action: str, name: str, *, source: str | Path | None = None,
           scope: str = 'user', home: Path | None = None, repo: Path | None = None) -> dict:
    """Preview or perform one selected skill action; never edits client MCP config."""
    home = home or Path.home()
    target = _target(name, scope=scope, home=home, repo=repo)
    state, receipt = _status(target)
    result = {'name': name, 'scope': scope, 'target': str(target), 'status': state,
              'installed_commit': receipt['source_commit'] if receipt else None}
    if action == 'remove':
        if state != 'managed':
            raise ValueError('Only an unchanged managed skill can be removed')
        backup = target.with_name(f'.{name}.aiw-removing')
        if backup.exists() or backup.is_symlink():
            raise ValueError('An interrupted skill removal needs manual reconciliation')
        target.rename(backup)
        shutil.rmtree(backup)
        return {**result, 'status': 'removed'}
    if action == 'doctor':
        return result
    selected, commit, files = _source(source, name)
    result['source_commit'] = commit
    result['source_matches_install'] = state == 'managed' and receipt['files'] == files
    if action == 'preview':
        return result
    if action not in {'install', 'update'}:
        raise ValueError('Use preview, install, update, remove, or doctor')
    if state == 'managed' and receipt['files'] == files:
        return {**result, 'status': 'unchanged'}
    if action == 'install' and state != 'absent':
        raise ValueError('Skill name already exists; use update only for an unchanged managed copy')
    if action == 'update' and state != 'managed':
        raise ValueError('Update requires an unchanged managed copy; preserve user edits')
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if parent.is_symlink() or parent.parent.is_symlink():
        raise ValueError('Skill destination contains a symlink')
    backup = target.with_name(f'.{name}.aiw-updating')
    if backup.exists() or backup.is_symlink():
        raise ValueError('An interrupted skill update needs manual reconciliation')
    temporary = _write_copy(selected, parent, name, commit, files)
    try:
        if action == 'update':
            target.rename(backup)
        temporary.rename(target)
    except BaseException:
        if backup.exists() and not target.exists():
            backup.rename(target)
        raise
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    if backup.exists():
        shutil.rmtree(backup)
    return {**result, 'status': 'installed' if action == 'install' else 'updated'}


def diagnostic(*, home: Path | None = None, repo: Path | None = None,
               source: Path | None = None, probe_client_version: bool = False) -> dict:
    """Read only: on-disk skill scopes and configured MCP name, never secret values."""
    home = home or Path.home()
    def checked(name, scope):
        try:
            return manage('doctor', name, scope=scope, home=home, repo=repo)
        except (OSError, ValueError, json.JSONDecodeError):
            return {'status': 'invalid', 'installed_commit': None}
    user_results = {name: checked(name, 'user') for name in SKILLS}
    project_results = {name: checked(name, 'repo') for name in SKILLS} if repo else None
    user = {name: result['status'] for name, result in user_results.items()}
    project = {name: result['status'] for name, result in project_results.items()} if project_results else None
    source_status = {}
    if source is not None:
        for name in SKILLS:
            try:
                _, commit, _ = _source(source, name)
                source_status[name] = {'status': 'committed', 'commit': commit}
            except (OSError, ValueError, subprocess.SubprocessError):
                source_status[name] = {'status': 'unavailable_or_dirty', 'commit': None}
    config = home / '.codex' / 'config.toml'
    try:
        data = tomllib.loads(config.read_text(encoding='utf-8')) if config.is_file() else {}
        servers = data.get('mcp_servers', {})
        server = servers.get('wb-mcp') if isinstance(servers, dict) else None
        if not isinstance(server, dict):
            mcp = 'not-configured'
        elif not isinstance(server.get('enabled', True), bool):
            mcp = 'configuration-unsupported'
        else:
            mcp = 'configured-enabled' if server.get('enabled', True) else 'configured-disabled'
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        mcp = 'configuration-unreadable'
    try:
        workbench_version = version('starforge-ai-workbench')
    except PackageNotFoundError:
        workbench_version = 'unknown'
    codex_version = 'not_checked'
    if probe_client_version:
        try:
            client = subprocess.run(['codex', '--version'], capture_output=True, text=True,
                                    timeout=5, check=False)
            codex_version = client.stdout.strip() if client.returncode == 0 else 'unknown'
        except (OSError, subprocess.SubprocessError):
            codex_version = 'unknown'
    return {'user_skills': user, 'repo_skills': project,
            'installed_commits': {'user': {name: row['installed_commit'] for name, row in user_results.items()},
                                  'repo': {name: row['installed_commit'] for name, row in project_results.items()} if project_results else None},
            'selected_source': source_status or None,
            'duplicate_names': [name for name in SKILLS if project and user[name] != 'absent' and project[name] != 'absent'],
            'wb_mcp_config': mcp, 'running_mcp_version': 'unknown', 'mcp_tool_listing': 'not_observed',
            'workbench_cli_package_version': workbench_version,
            'codex_cli_version': codex_version or 'unknown',
            'codex_session_discovery': 'not_observed', 'client_restart': 'not_performed'}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='ai-workbench skills')
    parser.add_argument('action', choices=['preview', 'install', 'update', 'remove', 'doctor'])
    parser.add_argument('names', nargs='*', choices=SKILLS)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--scope', choices=['user', 'repo'], default='user')
    parser.add_argument('--repo', type=Path)
    parser.add_argument('--probe-client-version', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.action == 'doctor':
            result = diagnostic(repo=args.repo, source=args.source,
                                probe_client_version=args.probe_client_version)
        else:
            if not args.names:
                raise ValueError('Select an exact Workbench skill name')
            if args.action in {'install', 'update', 'remove'} and len(args.names) != 1:
                raise ValueError('Install, update, and remove one skill at a time')
            result = [manage(args.action, name, source=args.source, scope=args.scope, repo=args.repo)
                      for name in args.names]
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        parser.exit(2, f'Workbench skills: {exc}\n')
