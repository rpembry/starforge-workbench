"""A small manual desktop dialog over the preview/apply contract."""
from __future__ import annotations

import shutil
import subprocess

from .favorite_apps import load_config


def _dialog(*args):
    try:
        result = subprocess.run(['zenity', *args], capture_output=True, text=True,
                                timeout=3600, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError('Favorite Apps could not open a graphical dialog; check your desktop session') from exc
    if result.returncode not in (0, 1) or result.stderr.strip():
        raise ValueError('Favorite Apps could not open a graphical dialog; check your desktop session')
    return result


def run_gui(restorer):
    if not shutil.which('zenity'):
        raise ValueError('The Favorite Apps window needs Zenity installed')
    favorites = load_config(restorer.config)
    if not favorites:
        _dialog('--info', '--title=Favorite Apps', '--text=No favorite apps are configured yet.')
        return 0
    selected = []
    while True:
        preview = restorer.preview([row['name'] for row in favorites])
        rows = []
        for item in preview['items']:
            label = {'launch': 'Ready to open', 'preserve': 'Already open',
                     'skip': 'Opening is still unverified', 'refuse': 'Cannot safely open'}[item['action']]
            if item.get('can_open_anyway'):
                label = 'Running status unknown'
            rows.extend(['TRUE' if item['name'] in selected else 'FALSE',
                         item['name'], label, item['evidence']])
        picked = _dialog('--list', '--checklist', '--title=Favorite Apps',
                         '--text=Select apps to open, then choose Preview. Already open apps stay open.',
                         '--width=900', '--height=580', '--ok-label=Preview',
                         '--separator=\n', '--column=Open', '--column=Favorite',
                         '--column=Status', '--column=Reason', *rows)
        if picked.returncode == 1:
            return 0
        selected = [name for name in picked.stdout.splitlines() if name]
        if not selected:
            continue
        selected_preview = restorer.preview(selected)
        details = '\n'.join(f"{item['name']}: {item['action']} — {item['evidence']}"
                            for item in selected_preview['items'])
        can_open = any(item['action'] == 'launch' for item in selected_preview['items'])
        unknown = [item for item in selected_preview['items'] if item.get('can_open_anyway')]
        if not can_open and not unknown:
            _dialog('--info', '--title=Favorite Apps', '--text=' + details)
            continue
        open_unknown = bool(unknown)
        if open_unknown:
            choice = _dialog('--question', '--title=Favorite Apps', '--width=700',
                             '--text=The running status of some selected apps cannot be verified. '
                             'Opening them may create another window. Open them anyway?\n\n' + details,
                             '--ok-label=Open anyway', '--cancel-label=Back')
        else:
            choice = _dialog('--question', '--title=Favorite Apps',
                             '--text=Open the ready apps shown below?\n\n' + details,
                             '--ok-label=Open apps', '--cancel-label=Back', '--width=700')
        if choice.returncode == 0:
            break
    try:
        result = restorer.apply(selected_preview['selection'], selected_preview['token'],
                                open_unknown=open_unknown)
    except ValueError:
        _dialog('--warning', '--title=Favorite Apps',
                '--text=An app changed after preview. Open Favorite Apps again to check it.')
        return 1
    summary = '\n'.join(f"{item['name']}: {item['result']} — {item['evidence']}"
                        for item in result['items'])
    _dialog('--info', '--title=Favorite Apps', '--text=' + summary, '--width=700')
    return 0
