"""Interactive user-run attachment proof and activation; never run by the agent."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import httpx

from .newsletter_download import GRANT, implementation_hash, validate_proof
from .newsletter_inline import convert, source_matches
from .newsletter_job import Remote, JobError, atomic_json, protected_read, private_dir, digest

DISCLOSURE = '''This program uses your existing Todoist token to read tasks and comments
in the configured Tech News project, and to retrieve their HTML attachments.
The token is sent only to api.todoist.com and exact HTTPS files.todoist.com
/user_upload/ URLs. Redirects are disabled. At most one validated signed CDN
hop is fetched without credentials (todoist.b-cdn.net or
d1ysz50cxb9zwl.cloudfront.net). No cookies, article links or tracking images
are fetched, and the token and newsletter content are never printed.
After a real matching attachment is read and parsed, this program enables
the converter every 15 minutes. It fills only blank matching newsletter
descriptions, preserving original attachments and prior verified receipts.
It creates no tasks. A tiny concurrent-edit race remains between GET and POST.
A successful no-op service run is NOT the required attachment proof.
'''


def command(args):
    result = subprocess.run(args, text=True, capture_output=True, timeout=330)
    if result.returncode:
        raise JobError('systemd_step_failed')
    return result.stdout.strip()


def live_proof(remote: Remote, project: str):
    started = time.monotonic()
    for task in remote.tasks(project)[:25]:
        if time.monotonic() - started > 180:
            raise JobError('proof_time_limit')
        if (task.get('project_id') != project or task.get('checked', False)
                or task.get('is_completed', False)):
            continue
        comments = [c for c in remote.comments(task['id'])
                    if (c.get('attachment') or {}).get('file_type') == 'text/html']
        if len(comments) != 1:
            continue
        comment = comments[0]
        attachment = comment['attachment']
        if comment.get('task_id') != task['id'] or attachment.get('file_name') != task.get('content'):
            continue
        html = remote.html(attachment.get('file_url'))
        # Existing nonblank tasks may prove retrieval. Nothing is cleared remotely.
        if not source_matches({**task, 'description':''}, comment, html, project):
            continue
        converted = convert(html)
        fresh = remote.task(task['id'])
        if (fresh.get('project_id') != project or fresh.get('content') != task.get('content')
                or fresh.get('checked', False) or fresh.get('is_completed', False)):
            raise JobError('proof_source_changed')
        return {'grant':GRANT, 'project_id':project, 'task_id':task['id'],
            'comment_id':comment['id'], 'source_hash':digest(html),
            'implementation_hash':implementation_hash(), 'live_attachment_read_and_parsed':True,
            'confirmation_mechanism':'personal interactive ACTIVATE handoff',
            'proved_at':datetime.now(timezone.utc).isoformat(), 'source_bytes':len(html.encode()),
            'description_utf16_units':len(converted.description.encode('utf-16-le'))//2,
            'links':converted.links, 'headings':converted.headings}
    raise JobError('representative_attachment_not_found')


def activate(config_path: Path, *, confirmed: bool, run=command, remote_factory=Remote):
    if not confirmed:
        raise JobError('personal_confirmation_required')
    config = json.loads(protected_read(config_path))
    directory = Path(config['state_dir'])
    if not directory.is_absolute():
        raise JobError('absolute_private_paths_required')
    private_dir(directory)
    with os.fdopen(os.open(directory/'activation.lock', os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW, 0o600),'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
        return _activate(config_path, run=run, remote_factory=remote_factory)


def _activate(config_path: Path, *, run, remote_factory):
    config = json.loads(protected_read(config_path))
    if config.get('enabled') is not False:
        raise JobError('configuration_must_start_disabled')
    project = config.get('project_id')
    if not isinstance(project, str) or not project.isalnum():
        raise JobError('invalid_project')
    if config.get('credential_grant') != 'read-newsletter-attachments-and-update-existing-descriptions.v1':
        raise JobError('existing_project_grant_required')
    directory = Path(config['state_dir'])
    token_path = Path(config['token_file'])
    if not directory.is_absolute() or not token_path.is_absolute():
        raise JobError('absolute_private_paths_required')
    private_dir(directory)
    home = Path.home()
    for name in ['newsletter-inline.service','newsletter-inline.timer']:
        installed = home / '.config/systemd/user' / name
        reviewed = Path(__file__).parents[2] / 'deploy' / name
        if installed.read_bytes() != reviewed.read_bytes():
            raise JobError('installed_unit_differs_from_reviewed')
    run(['systemd-analyze','--user','verify', str(home/'.config/systemd/user/newsletter-inline.service'),
         str(home/'.config/systemd/user/newsletter-inline.timer')])
    token = protected_read(token_path).strip()
    if not token or any(c.isspace() for c in token):
        raise JobError('invalid_token')
    # This is the user-run credential-bearing step, before any enabling/write.
    with httpx.Client(timeout=10, trust_env=False, follow_redirects=False) as client:
        proof = live_proof(remote_factory(token, client, attachment_auth=True), project)
    validate_proof(proof, project)
    atomic_json(directory/'attachment-proof.json', proof)
    approved = {**config, 'attachment_grant':GRANT, 'enabled':True}
    atomic_json(config_path, approved)
    try:
        run(['systemctl','--user','start','newsletter-inline.service'])
        result = run(['systemctl','--user','show','newsletter-inline.service','-p','Result','--value'])
        if result != 'success':
            raise JobError('initial_service_run_failed')
        report = json.loads(protected_read(directory/'last-run.json'))
        if (report.get('status') != 'success' or report.get('finished_at', 0)
                < datetime.fromisoformat(proof['proved_at']).timestamp()):
            raise JobError('fresh_service_report_required')
        run(['systemctl','--user','enable','--now','newsletter-inline.timer'])
        if run(['systemctl','--user','is-enabled','newsletter-inline.timer']) != 'enabled':
            raise JobError('timer_not_enabled')
        if run(['systemctl','--user','is-active','newsletter-inline.timer']) != 'active':
            raise JobError('timer_not_active')
        next_run = run(['systemctl','--user','show','newsletter-inline.timer','-p','NextElapseUSecRealtime','--value'])
        if not next_run:
            raise JobError('next_timer_trigger_missing')
        result = {'status':'activated', 'live_attachment_read_and_parsed':True,
                  'source_bytes':proof['source_bytes'], 'headings':proof['headings'], 'links':proof['links'],
                  'initial_service_report':report, 'next_run':next_run}
        atomic_json(directory/'activation.json', result)
        return result
    except BaseException:
        try:
            atomic_json(config_path, {**approved, 'enabled':False})
        finally:
            try:
                run(['systemctl','--user','disable','--now','newsletter-inline.timer'])
            except Exception:
                pass
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    args = parser.parse_args()
    expected = Path.home()/'.config/newsletter-inline/config.json'
    if args.config.absolute() != expected or not sys.stdin.isatty():
        print('Run personally in an interactive terminal with the reviewed config path.')
        return 1
    print(DISCLOSURE)
    confirmed = input('Type ACTIVATE to run this credential-bearing proof and activation: ') == 'ACTIVATE'
    try:
        print(json.dumps(activate(args.config, confirmed=confirmed)))
        return 0
    except (Exception, KeyboardInterrupt):
        print(json.dumps({'status':'not_activated', 'error':'proof_or_configuration_or_service_failed',
                          'check_timer_remains_disabled':True}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
