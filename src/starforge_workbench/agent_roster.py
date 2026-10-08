"""Private specialist roster and exact, explicit Codex handoffs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat

import yaml

from starforge_workbench import cli, codex_daemon


KEY = re.compile(r'^[A-Za-z0-9._~-]{16,128}$')


def _private_text(path, limit):
    path = Path(path).expanduser()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > limit):
            raise ValueError('Private handoff input must be owned by you, mode 0600, and size-limited')
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError('Private handoff input exceeds size limit')
        return data.decode('utf-8')


def roster(path):
    data = yaml.safe_load(_private_text(path, 16384))
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('agents'), list):
        raise ValueError('Invalid private agent roster')
    result = {}
    for row in data['agents']:
        if not isinstance(row, dict) or set(row) != {'context_id', 'description'}:
            raise ValueError('Invalid agent roster entry')
        context_id, description = row['context_id'], row['description']
        if (not isinstance(context_id, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,47}', context_id)
                or not isinstance(description, str) or not 1 <= len(description) <= 500
                or any(ord(ch) < 32 for ch in description) or context_id in result):
            raise ValueError('Invalid or duplicate agent roster entry')
        result[context_id] = description
    return result


def _context(manifest, roster_path, context_id):
    roles = roster(roster_path)
    if context_id not in roles:
        raise ValueError('Context is not in the private agent roster')
    matches = [c for c in cli.load(manifest)['contexts'] if c['id'] == context_id and c['enabled']]
    if len(matches) != 1:
        raise ValueError('Roster context is missing, disabled, or ambiguous')
    c = matches[0]
    if c['provider'] != 'codex' or not c.get('codex_remote_daemon'):
        raise ValueError('Roster context has no supported exact Codex daemon transport')
    return c


def list_agents(manifest, roster_path):
    contexts = {c['id']: c for c in cli.load(manifest)['contexts']}
    return [{'context_id': identity, 'title': contexts.get(identity, {}).get('title'),
             'description': description,
             'routable': bool(contexts.get(identity, {}).get('enabled') and
                              contexts.get(identity, {}).get('codex_remote_daemon'))}
            for identity, description in roster(roster_path).items()]


def preview(manifest, roster_path, context_id):
    c = _context(manifest, roster_path, context_id)
    binding = cli.saved_session(c)
    if not binding:
        return {'outcome': 'unavailable', 'reason': 'no_exact_binding', 'context_id': context_id}
    try:
        status = codex_daemon.thread_status(binding['id'])
    except Exception:
        return {'outcome': 'unknown', 'reason': 'daemon_status_unavailable', 'context_id': context_id}
    return {'outcome': 'ready' if status == 'idle' else 'unavailable',
            'context_id': context_id, 'session_id': binding['id'], 'status': status}


def send(manifest, roster_path, context_id, expected_session_id, message, key):
    if not isinstance(context_id, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,47}', context_id):
        raise ValueError('Select a valid roster context ID')
    if not KEY.fullmatch(key):
        raise ValueError('Use a stable 16..128 character handoff key')
    if not isinstance(message, str) or not message.strip() or len(message) > 4000:
        raise ValueError('Handoff must contain 1..4000 characters')
    if not codex_daemon.THREAD_ID.fullmatch(expected_session_id):
        raise ValueError('Select an exact Codex session ID')
    digest = hashlib.sha256(message.encode()).hexdigest()
    location = cli.STATE/'agent-handoffs'/(hashlib.sha256(key.encode()).hexdigest()+'.json')
    with cli.lock('agent-handoff-'+hashlib.sha256(key.encode()).hexdigest()), \
            cli.lock('agent-target-'+context_id):
        prior = cli.read_state(location)
        if prior:
            if any(prior.get(k) != v for k, v in {'context_id': context_id,
                    'session_id': expected_session_id, 'message_sha256': digest}.items()):
                raise ValueError('Handoff key was already used for a different request')
            return prior
        c = _context(manifest, roster_path, context_id)
        binding = cli.saved_session(c)
        if not binding or binding['id'] != expected_session_id:
            return {'outcome': 'unavailable', 'reason': 'binding_changed'}
        target = preview(manifest, roster_path, context_id)
        if target.get('outcome') != 'ready' or target.get('session_id') != expected_session_id:
            return target
        if cli.saved_session(c)['id'] != expected_session_id:
            return {'outcome': 'unavailable', 'reason': 'binding_changed'}
        record = {'outcome': 'uncertain', 'phase': 'requested', 'context_id': context_id,
                  'session_id': expected_session_id, 'message_sha256': digest}
        cli.atomic(location, record)
        try:
            turn_id = codex_daemon.start_turn(expected_session_id, message)
        except Exception:
            pass
        else:
            record.update(outcome='accepted', turn_id=turn_id)
        cli.atomic(location, record)
        return record


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest', type=Path, default=cli.default_manifest())
    p.add_argument('--roster', type=Path, default=Path.home()/'.config/starforge-ai-workbench/agent-roster.yaml')
    p.add_argument('command', choices=('list', 'preview', 'send'))
    p.add_argument('context_id', nargs='?')
    p.add_argument('--session-id')
    p.add_argument('--message-file', type=Path)
    p.add_argument('--key')
    args = p.parse_args(argv)
    if args.command == 'list':
        result = list_agents(args.manifest, args.roster)
    elif not args.context_id:
        p.error('preview/send requires a context ID')
    elif args.command == 'preview':
        result = preview(args.manifest, args.roster, args.context_id)
    else:
        if not args.session_id or not args.message_file or not args.key:
            p.error('send requires --session-id, --message-file, and --key')
        result = send(args.manifest, args.roster, args.context_id, args.session_id,
                      _private_text(args.message_file, 8192), args.key)
    print(json.dumps(result))
