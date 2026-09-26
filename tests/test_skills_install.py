import json
from pathlib import Path
import subprocess

import pytest

from starforge_workbench.cli import main as launcher_main
from starforge_workbench.skills_install import diagnostic, manage


NAME = 'aiw-flow-capture'


def git(path, *args):
    return subprocess.run(['git', '-C', str(path), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def source(tmp_path):
    root = tmp_path / 'pinned-source'
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(root)], check=True)
    git(root, 'config', 'user.name', 'Example Operator')
    git(root, 'config', 'user.email', 'operator@example.com')
    skill = root / 'skills' / NAME
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('---\nname: aiw-flow-capture\ndescription: Capture selected work.\n---\n\nFirst revision.\n')
    git(root, 'add', 'skills')
    git(root, 'commit', '-qm', 'First skill')
    return root


def test_install_update_remove_preserve_edits_and_pin_commit(source, tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    target = home / '.agents' / 'skills' / NAME
    preview = manage('preview', NAME, source=source, home=home)
    assert preview['status'] == 'absent' and not target.exists()
    first = manage('install', NAME, source=source, home=home)
    assert first['status'] == 'installed'
    assert first['source_commit'] == git(source, 'rev-parse', 'HEAD')
    assert manage('install', NAME, source=source, home=home)['status'] == 'unchanged'
    (target / 'SKILL.md').write_text('local user edit\n')
    assert diagnostic(home=home)['user_skills'][NAME] == 'modified'
    with pytest.raises(ValueError, match='unchanged managed'):
        manage('update', NAME, source=source, home=home)
    with pytest.raises(ValueError, match='unchanged managed'):
        manage('remove', NAME, home=home)
    assert (target / 'SKILL.md').read_text() == 'local user edit\n'
    (target / 'SKILL.md').write_text((source / 'skills' / NAME / 'SKILL.md').read_text())
    (source / 'skills' / NAME / 'SKILL.md').write_text('---\nname: aiw-flow-capture\ndescription: Capture selected work.\n---\n\nSecond revision.\n')
    with pytest.raises(ValueError, match='uncommitted'):
        manage('update', NAME, source=source, home=home)
    git(source, 'add', 'skills')
    git(source, 'commit', '-qm', 'Second skill')
    updated = manage('update', NAME, source=source, home=home)
    assert updated['status'] == 'updated'
    assert updated['source_commit'] == git(source, 'rev-parse', 'HEAD')
    assert 'Second revision' in (target / 'SKILL.md').read_text()
    assert manage('remove', NAME, home=home)['status'] == 'removed'
    assert not target.exists()


def test_collisions_duplicates_and_read_only_diagnostic(source, tmp_path, capsys, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    target = home / '.agents' / 'skills' / NAME
    target.mkdir(parents=True)
    (target / 'SKILL.md').write_text('unrelated user skill\n')
    with pytest.raises(ValueError, match='already exists'):
        manage('install', NAME, source=source, home=home)
    assert (target / 'SKILL.md').read_text() == 'unrelated user skill\n'
    repo = tmp_path / 'project'
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(repo)], check=True)
    assert manage('install', NAME, source=source, scope='repo', repo=repo, home=home)['status'] == 'installed'
    config = home / '.codex' / 'config.toml'
    config.parent.mkdir()
    config.write_text('[mcp_servers.wb-mcp]\ncommand = "wb-mcp"\n[mcp_servers.wb-mcp.env]\nSECRET = "not for output"\n')
    original_config = config.read_bytes()
    assert manage('update', NAME, source=source, scope='repo', repo=repo, home=home)['status'] == 'unchanged'
    assert config.read_bytes() == original_config
    report = diagnostic(home=home, repo=repo)
    assert report['duplicate_names'] == [NAME]
    assert report['wb_mcp_config'] == 'configured-enabled'
    assert report['running_mcp_version'] == 'unknown'
    assert 'not for output' not in json.dumps(report)
    monkeypatch.setattr(Path, 'home', lambda: home)
    assert launcher_main(['skills', 'preview', NAME, '--source', str(source)]) == 0
    assert json.loads(capsys.readouterr().out)[0]['source_commit'] == git(source, 'rev-parse', 'HEAD')
    with pytest.raises(SystemExit, match='2'):
        launcher_main(['skills', 'install', NAME, 'aiw-flow-resume', '--source', str(source)])


def test_unpinned_and_interrupted_sources_are_rejected(source, tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    with pytest.raises(ValueError, match='pinned local'):
        manage('install', NAME, home=home)
    backup = home / '.agents' / 'skills' / f'.{NAME}.aiw-updating'
    backup.mkdir(parents=True)
    assert manage('doctor', NAME, home=home)['status'] == 'interrupted'
    with pytest.raises(ValueError, match='already exists'):
        manage('install', NAME, source=source, home=home)
