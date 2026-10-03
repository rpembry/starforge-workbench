"""A small manual desktop dialog over the preview/apply contract."""
from __future__ import annotations

import shutil
import subprocess

from .favorite_apps import load_config


UNRESOLVED_HELP = ('A prior launch may still be opening. Check the app’s existing windows. '
                   'A retry in this boot may create a duplicate; it requires a separate confirmation '
                   'and is recorded locally. You can also return to the list to refresh status.')


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
    # A visible default selection is still only a proposal: no launch happens
    # until the user previews and confirms it.
    selected = [row['name'] for row in favorites]
    while True:
        preview = restorer.preview([row['name'] for row in favorites])
        rows = []
        for item in preview['items']:
            label = {'launch': 'Ready to open', 'preserve': 'Already open',
                     'skip': 'Prior launch unverified — retry needs confirmation',
                     'refuse': 'Cannot safely open'}[item['action']]
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
        details = '\n'.join(
            f"{item['name']}: {'Prior launch unresolved' if item['action'] == 'skip' else item['action']}"
            f" — {item['evidence']}" for item in selected_preview['items'])
        blocked = any(item['action'] == 'skip' for item in selected_preview['items'])
        if blocked:
            details += '\n\n' + UNRESOLVED_HELP
        can_open = any(item['action'] == 'launch' for item in selected_preview['items'])
        unknown = [item for item in selected_preview['items'] if item.get('can_open_anyway')]
        if not can_open and not unknown and not blocked:
            _dialog('--info', '--title=Favorite Apps', '--text=' + details,
                    '--ok-label=Refresh status', '--width=700')
            continue
        retry_unverified = False
        if blocked:
            retry_names = ', '.join(item['name'] for item in selected_preview['items']
                                    if item['action'] == 'skip')
            retry_choice = _dialog(
                '--question', '--title=Favorite Apps', '--width=700',
                '--text=Check that these apps are not already open before retrying: '
                + retry_names + '\n\nTheir previous launch may still be starting. '
                'Retrying can create duplicate windows. This retry will be recorded locally.\n\n'
                + details,
                '--ok-label=I checked; retry', '--cancel-label=Back')
            if retry_choice.returncode == 1:
                continue
            retry_unverified = True
        open_unknown = bool(unknown or blocked)
        if unknown:
            choice = _dialog('--question', '--title=Favorite Apps', '--width=700',
                             '--text=The running status of some selected apps cannot be verified. '
                             'Opening them may create another window. Open them anyway?\n\n' + details,
                             '--ok-label=Open anyway', '--cancel-label=Back')
        elif not blocked:
            choice = _dialog('--question', '--title=Favorite Apps',
                             '--text=Open the ready apps shown below?\n\n' + details,
                             '--ok-label=Open apps', '--cancel-label=Back', '--width=700')
        else:
            break
        if choice.returncode == 0:
            break
    try:
        result = restorer.apply(selected_preview['selection'], selected_preview['token'],
                                open_unknown=open_unknown,
                                retry_unverified=retry_unverified)
    except ValueError:
        _dialog('--warning', '--title=Favorite Apps',
                '--text=An app changed after preview. Open Favorite Apps again to check it.')
        return 1
    summary = '\n'.join(
        f"{item['name']}: {'Not reopened; prior launch remains unverified' if item['result'] == 'uncertain' and not item['launch_requested'] else item['result']}"
        f" — {item['evidence']}" for item in result['items'])
    if any(item['result'] == 'uncertain' and not item['launch_requested']
           for item in result['items']):
        summary += '\n\n' + UNRESOLVED_HELP
    _dialog('--info', '--title=Favorite Apps', '--text=' + summary, '--width=700')
    return 0
