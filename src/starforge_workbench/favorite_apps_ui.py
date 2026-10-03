"""A small manual desktop dialog over the preview/apply contract."""
from __future__ import annotations

import shutil
import subprocess

from .favorite_apps import load_config


UNRESOLVED_HELP = ('A prior launch may still be opening. Check the app’s existing windows. '
                   'A retry in this boot may create a duplicate; it requires a separate confirmation '
                   'and is recorded locally. You can also return to the list to refresh status.')
PREVIEW_KEYS = ('launch', 'preserve', 'skip', 'refuse', 'unknown')
RESULT_KEYS = ('launch_requested', 'launcher_accepted', 'verified_ready',
               'already_present', 'unresolved', 'refused')


def _preview_counts(preview):
    items = preview['items'] if preview else []
    counts = {key: 0 for key in PREVIEW_KEYS}
    for item in items:
        counts[item['action']] += 1
        if item.get('can_open_anyway'):
            counts['unknown'] += 1
    return counts


def _result_counts(result):
    counts = {key: 0 for key in RESULT_KEYS}
    for item in result['items']:
        counts['launch_requested'] += bool(item['launch_requested'])
        counts['launcher_accepted'] += item['launcher_accepted'] is True
        counts['verified_ready'] += item['result'] == 'verified_ready'
        counts['already_present'] += item['result'] == 'already_present'
        counts['unresolved'] += item['result'] == 'uncertain' and not item['launch_requested']
        counts['refused'] += item['result'] == 'refused'
    return counts


def _headline(counts, selected_count):
    attempted = counts['launch_requested']
    if attempted == 0:
        if counts['already_present'] == selected_count:
            return 'All selected apps were already open. No new launch was requested.'
        return 'No apps were opened. Some selected apps were blocked or could not be verified.'
    return (f"{attempted} launch request(s) made; {counts['launcher_accepted']} accepted by the desktop; "
            f"{counts['verified_ready']} verified ready. An accepted request does not prove the app opened.")


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
    context = {'selected_count': 0, 'preview': None, 'recorded': False}
    try:
        return _run_gui(restorer, context)
    except ValueError as exc:
        if not context['recorded']:
            reason = ('display_error' if 'graphical dialog' in str(exc) else
                      'configuration_error' if context['preview'] is None else 'apply_error')
            try:
                restorer.record_attempt(
                    phase='error', selected_count=context['selected_count'],
                    preview_counts=_preview_counts(context['preview']),
                    result_counts={key: 0 for key in RESULT_KEYS}, failure_reason=reason)
            except (OSError, ValueError) as audit_exc:
                raise ValueError('Favorite Apps failed and could not save its private attempt record') from audit_exc
        raise


def _run_gui(restorer, context):
    if not shutil.which('zenity'):
        raise ValueError('The Favorite Apps window needs Zenity installed')
    favorites = load_config(restorer.config)
    if not favorites:
        context['recorded'] = True
        restorer.record_attempt(phase='empty_configuration', selected_count=0,
                                preview_counts={key: 0 for key in PREVIEW_KEYS},
                                result_counts={key: 0 for key in RESULT_KEYS})
        _dialog('--info', '--title=Favorite Apps', '--text=No favorite apps are configured yet.')
        return 0
    # Default to apps whose absence was verified. Back keeps later choices.
    selected = None
    while True:
        preview = restorer.preview([row['name'] for row in favorites])
        context['preview'] = preview
        if selected is None:
            selected = [item['name'] for item in preview['items']
                        if item['action'] == 'launch']
            context['selected_count'] = len(selected)
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
                         '--text=Ready to open apps are checked. Select others if needed, then choose Preview. Already open apps stay open.',
                         '--width=900', '--height=580', '--ok-label=Preview',
                         '--separator=\n', '--column=Open', '--column=Favorite',
                         '--column=Status', '--column=Reason', *rows)
        if picked.returncode == 1:
            context['recorded'] = True
            restorer.record_attempt(phase='cancelled', selected_count=len(selected),
                                    preview_counts=_preview_counts(preview),
                                    result_counts={key: 0 for key in RESULT_KEYS})
            return 0
        selected = [name for name in picked.stdout.splitlines() if name]
        context['selected_count'] = len(selected)
        if not selected:
            context['recorded'] = True
            restorer.record_attempt(phase='no_launch', selected_count=0,
                                    preview_counts=_preview_counts(preview),
                                    result_counts={key: 0 for key in RESULT_KEYS})
            context['recorded'] = False
            _dialog('--info', '--title=Favorite Apps', '--width=700',
                    '--ok-label=Back to favorites',
                    '--text=No apps are selected, so nothing was opened. '
                    'Only apps verified closed start checked. Select an app and choose Preview. '
                    'An app marked Running status unknown may already be open; opening it requires a separate confirmation.')
            continue
        selected_preview = restorer.preview(selected)
        context['preview'] = selected_preview
        details = '\n'.join(
            f"{item['name']}: {'Prior launch unresolved' if item['action'] == 'skip' else item['action']}"
            f" — {item['evidence']}" for item in selected_preview['items'])
        blocked = any(item['action'] == 'skip' for item in selected_preview['items'])
        if blocked:
            details += '\n\n' + UNRESOLVED_HELP
        can_open = any(item['action'] == 'launch' for item in selected_preview['items'])
        unknown = [item for item in selected_preview['items'] if item.get('can_open_anyway')]
        if not can_open and not unknown and not blocked:
            context['recorded'] = True
            restorer.record_attempt(phase='no_launch', selected_count=len(selected),
                                    preview_counts=_preview_counts(selected_preview),
                                    result_counts={key: 0 for key in RESULT_KEYS})
            context['recorded'] = False
            headline = ('All selected apps were already open. No new launch was requested.'
                        if all(item['action'] == 'preserve' for item in selected_preview['items'])
                        else 'No apps were opened. Some selected apps could not be safely launched.')
            _dialog('--info', '--title=Favorite Apps', '--text=' + headline + '\n\n' + details,
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
        context['recorded'] = True
        restorer.record_attempt(phase='error', selected_count=len(selected),
                                preview_counts=_preview_counts(selected_preview),
                                result_counts={key: 0 for key in RESULT_KEYS},
                                failure_reason='identity_changed')
        _dialog('--warning', '--title=Favorite Apps',
                '--text=An app changed after preview. Open Favorite Apps again to check it.')
        return 1
    counts = _result_counts(result)
    audit_error = False
    context['recorded'] = True
    try:
        restorer.record_attempt(phase='completed', selected_count=len(selected),
                                preview_counts=_preview_counts(selected_preview),
                                result_counts=counts)
    except (OSError, ValueError):
        audit_error = True
    summary = _headline(counts, len(selected)) + '\n\n'
    summary += '\n'.join(
        f"{item['name']}: {'Not reopened; prior launch remains unverified' if item['result'] == 'uncertain' and not item['launch_requested'] else item['result']}"
        f" — {item['evidence']}" for item in result['items'])
    if any(item['result'] == 'uncertain' and not item['launch_requested']
           for item in result['items']):
        summary += '\n\n' + UNRESOLVED_HELP
    if audit_error:
        summary += '\n\nThe private attempt summary could not be saved; review this result before retrying.'
    _dialog('--info', '--title=Favorite Apps', '--text=' + summary, '--width=700')
    return 1 if audit_error else 0
