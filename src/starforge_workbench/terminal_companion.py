"""Focus-only Codex companion core; no capture, send, launch or shell injection.

The shipped CLI has no current-TUI identity adapter and refuses live activation.
Trusted fixture composition exercises focus under an identity fence. A future
adapter must establish the actual displayed conversation, not a catalog record,
resume argv, caller label or static tmux option. No adapter is loaded from config.
"""
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re

from .diagnostic_release import _read
from .docker_worker import atomic, attempt_lock, private_directory

SHELLS = frozenset({'sh', 'bash', 'zsh', 'fish'})


class CompanionError(ValueError):
    def __init__(self):
        super().__init__('Terminal companion unavailable')


def capabilities():
    """No live identity support is claimed merely because Codex is installed."""
    return {'protocol': 'terminal.companion.v1', 'status': 'unsupported',
            'reason': 'current_tui_identity_unavailable', 'provider': 'codex',
            'automatic_capture': False, 'automatic_send': False, 'automatic_launch': False}


def shortcut_config(context_id):
    """Descriptor only; never installs, replaces or sends a terminal key event."""
    if type(context_id) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,99}', context_id):
        raise CompanionError()
    return {'preferred_chord': 'Cmd+I', 'linux_candidate': 'Super+I', 'installed': False,
            'argv': ['aiw', 'companion', 'toggle', context_id],
            'requires': ['verified_terminal_key_event', 'no_existing_binding_conflict',
                         'supported_current_tui_identity', 'explicit_local_installation'],
            'clipboard_editor': 'unchanged'}


@dataclass(frozen=True)
class Pane:
    server: str
    server_generation: str
    session: str
    window: str
    pane: str
    pid: int
    command: str
    active: bool

    def validate(self):
        if (type(self.server) is not str or not self.server.startswith('/') or len(self.server) > 512
                or '\n' in self.server or '\x00' in self.server
                or type(self.server_generation) is not str
                or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', self.server_generation)
                or type(self.session) is not str or not re.fullmatch(r'\$[0-9]+', self.session)
                or type(self.window) is not str or not re.fullmatch(r'@[0-9]+', self.window)
                or type(self.pane) is not str or not re.fullmatch(r'%[0-9]+', self.pane)
                or type(self.pid) is not int or not 0 < self.pid < 2**31
                or type(self.command) is not str or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', self.command)
                or type(self.active) is not bool or not self.active):
            raise CompanionError()
        return self


@dataclass(frozen=True)
class ClientView:
    client: str
    incarnation: str
    pane: Pane

    def validate(self):
        if (type(self.client) is not str or not re.fullmatch(r'/dev/pts/[0-9]+', self.client)
                or type(self.incarnation) is not str
                or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', self.incarnation)
                or type(self.pane) is not Pane):
            raise CompanionError()
        self.pane.validate()
        return self


@dataclass(frozen=True)
class DisplayedConversation:
    """Owner-probe evidence only; never accepted as a CLI/tool argument."""
    thread_id: str
    generation: str
    pane: Pane

    def validate(self):
        if (type(self.thread_id) is not str
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,99}', self.thread_id)
                or type(self.generation) is not str
                or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', self.generation)
                or type(self.pane) is not Pane):
            raise CompanionError()
        self.pane.validate()
        return self


class Companion:
    """One existing client's focus ticket; identity/lifecycle stay with their owners.

    port supplies current(), observe(pane_id), focus(client_id, session_id).
    identity supplies scope(thread_id, pane) and read() under that same scope.
    The trusted identity scope must serialize current-conversation changes with
    focus; repeated unsynchronized catalog reads do not satisfy that contract.
    State contains metadata only. Uncertain/crashed focus is never replayed.
    """
    def __init__(self, state: Path, *, port, identity, target: Pane, thread_id: str):
        try:
            if identity is None or type(target) is not Pane or type(thread_id) is not str:
                raise CompanionError()
            target.validate()
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,99}', thread_id):
                raise CompanionError()
            self.state = private_directory(state)
            self.port, self.identity, self.target, self.thread_id = port, identity, target, thread_id
        except Exception:
            raise CompanionError() from None

    def _same_pane(self, expected):
        actual = self.port.observe(expected.pane)
        if type(actual) is not Pane or actual.validate() != expected:
            raise CompanionError()

    def _witness(self):
        value = self.identity.read()
        if (type(value) is not DisplayedConversation or value.validate().thread_id != self.thread_id
                or value.pane != self.target):
            raise CompanionError()
        return value

    def _client(self):
        view = self.port.current()
        if type(view) is not ClientView:
            raise CompanionError()
        return view.validate()

    def _save(self, state):
        if len((json.dumps(state, sort_keys=True)+'\n').encode()) > 8192:
            raise CompanionError()
        atomic(self.state/'focus.json', state)

    def _load(self):
        value = _read(self.state/'focus.json')
        if (value.keys() != {'version', 'phase', 'client', 'incarnation', 'source', 'target', 'witness'}
                or type(value['version']) is not int or value['version'] != 1
                or value['phase'] not in {'intent', 'away', 'return_intent', 'returned', 'uncertain'}):
            raise CompanionError()
        source, target = Pane(**value['source']).validate(), Pane(**value['target']).validate()
        witness = DisplayedConversation(value['witness']['thread_id'], value['witness']['generation'],
                                        Pane(**value['witness']['pane'])).validate()
        ClientView(value['client'], value['incarnation'], source).validate()
        if (target != self.target or witness.pane != target or witness.thread_id != self.thread_id
                or source.command not in SHELLS or source.session == target.session
                or source.server != target.server or source.server_generation != target.server_generation):
            raise CompanionError()
        return value, source, witness

    def toggle(self):
        try:
            with attempt_lock(self.state):
                current = self._client()
                if current.pane.server != self.target.server or current.pane.server_generation != self.target.server_generation:
                    raise CompanionError()
                with self.identity.scope(self.thread_id, self.target):
                    witness = self._witness()
                    self._same_pane(self.target)
                    path = self.state/'focus.json'
                    previous = self._load() if path.exists() or path.is_symlink() else None
                    if previous and previous[0]['phase'] not in {'away', 'returned'}:
                        raise CompanionError()  # preserve uncertain state, never repeat an intent
                    returning = previous is not None and previous[0]['phase'] == 'away'
                    if returning:
                        state, source, prior_witness = previous
                        if (current.client != state['client'] or current.incarnation != state['incarnation']
                                or current.pane != self.target or witness != prior_witness):
                            raise CompanionError()
                        self._same_pane(source)
                        destination = source
                    else:
                        source = current.pane
                        if source.command not in SHELLS or source.session == self.target.session:
                            raise CompanionError()
                        self._same_pane(source)
                        destination = self.target
                        state = {'version': 1, 'phase': 'intent', 'client': current.client,
                                 'incarnation': current.incarnation, 'source': asdict(source),
                                 'target': asdict(self.target), 'witness': asdict(witness)}
                    if self._client() != current or self._witness() != witness:
                        raise CompanionError()
                    state['phase'] = 'return_intent' if returning else 'intent'
                    self._save(state)  # durable metadata intent before focus, no payload
                    try:
                        # Durable write may yield while the human changes focus.
                        if self._client() != current or self._witness() != witness:
                            raise CompanionError()
                        self._same_pane(source)
                        self._same_pane(self.target)
                        self.port.focus(current.client, destination.session)
                        after = self._client()
                        if (after != ClientView(current.client, current.incarnation, destination)
                                or self._witness() != witness):
                            raise CompanionError()
                        self._same_pane(source)
                        self._same_pane(self.target)
                    except Exception:
                        state['phase'] = 'uncertain'; self._save(state)
                        raise CompanionError() from None
                    state['phase'] = 'returned' if returning else 'away'
                    self._save(state)
                    return {'protocol': 'terminal.companion.v1',
                            'status': 'returned' if returning else 'revealed'}
        except Exception:
            raise CompanionError() from None


class TmuxFocusPort:
    """Owner-composed metadata probes and one fixed tmux focus command only.

    Existing launcher/fixture socket selection owns the server. No arbitrary
    operation, shell command, text capture or keystroke API is provided here.
    """
    def __init__(self, *, current, observe, tmux):
        self._current, self._observe, self._tmux = current, observe, tmux

    def current(self): return self._current()
    def observe(self, pane): return self._observe(pane)

    def focus(self, client, session):
        if (type(client) is not str or not re.fullmatch(r'/dev/pts/[0-9]+', client)
                or type(session) is not str or not re.fullmatch(r'\$[0-9]+', session)):
            raise CompanionError()
        self._tmux('switch-client', '-c', client, '-t', session)


def main(argv, *, load, default_manifest, resolve_context):
    """CLI readiness/shortcut descriptor only until a live identity owner exists."""
    import argparse
    parser = argparse.ArgumentParser(prog='aiw companion', add_help=False, exit_on_error=False)
    parser.add_argument('--manifest', type=Path, default=default_manifest())
    parser.add_argument('operation', choices=['status', 'toggle', 'shortcut'])
    parser.add_argument('contexts', nargs='+')
    try:
        args, unknown = parser.parse_known_args(argv)
        if unknown:
            raise CompanionError()
        context = resolve_context(load(args.manifest.resolve())['contexts'], args.contexts)
        if context.get('provider') != 'codex' or context.get('enabled') is not True:
            raise CompanionError()
        if args.operation == 'toggle':
            raise CompanionError()  # no caller-provided thread ID/flag can enable focus
        result = shortcut_config(context['id']) if args.operation == 'shortcut' else capabilities()
        print(json.dumps(result, sort_keys=True))
        return result
    except Exception:
        raise CompanionError() from None
