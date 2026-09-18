"""Resolve only a collector-owned live registration to its local OpenCode ID."""
import hashlib
import json
from pathlib import Path
import re
import socket

from workbench.collector import (_binding_record, _registration_state,
                                 collect, configured_contexts)


REGISTERED_ID = re.compile(r'[A-Za-z0-9_-]{16,128}')


def resolve_opencode_registration(registered_id, manifest, registration_state,
                                  launcher_state, selected=()):
    """Return the exact local ses_ ID only for the current verified generation.

    None means missing/changed evidence; it never authorizes a guessed target.
    The binding ID is returned to the local caller only, never to Workbench.
    """
    if not isinstance(registered_id, str) or not REGISTERED_ID.fullmatch(registered_id):
        return None
    if not Path(registration_state).is_file():
        return None
    state = _registration_state(registration_state)
    matches = [(context_id, item) for context_id, item in state['contexts'].items()
               if item['id'] == registered_id]
    if len(matches) != 1:
        return None
    context_id, item = matches[0]
    context = configured_contexts(manifest, selected).get(context_id)
    if not context or context['provider'] != 'opencode' or item['provider'] != 'opencode':
        return None
    runs = [run for run in collect(manifest, (context_id,)) if run['context'] == context_id]
    if len(runs) != 1:
        return None
    record = _binding_record(context, Path(launcher_state))
    if (not record or not isinstance(record['id'], str) or
        not record['id'].startswith('ses_') or not REGISTERED_ID.fullmatch(record['id'])):
        return None
    generation = hashlib.sha256(json.dumps({
        'run': runs[0]['source_id'], 'binding': record['generation'],
        'host': socket.gethostname(), 'display_name': context['title'],
        'provider': context['provider'],
    }, sort_keys=True).encode()).hexdigest()
    return record['id'] if generation == item['generation'] else None
