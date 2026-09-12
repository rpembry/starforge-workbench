"""Explicit manual launcher. Runtime state never authorizes work."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import threading
import uuid

import jsonschema
import yaml

ROOT = Path(__file__).resolve().parents[2]
SELF = ROOT / 'bin/ai-workbench'
HOME = Path.home()
STATE = HOME / '.local/state/starforge-ai-workbench'
SERVER = 'starforge-ai-workbench'
TMUX_SOCKET = None  # Optional explicit socket, primarily for isolated integration tests.
TMUX_TIMEOUT = 3
PROVIDERS = {'opencode': str(HOME/'.opencode/bin/opencode'), 'codex': str(HOME/'bin/codex'), 'claude': str(HOME/'.local/share/npm-global/lib/node_modules/@anthropic-ai/claude-code/node_modules/@anthropic-ai/claude-code-linux-x64/claude'),
             'antigravity': str(HOME/'.local/bin/agy'), 'ollama': str(HOME/'.local/bin/ollama')}
# Prefer the user's installed command; retain fallbacks for existing setups.
PROVIDERS = {name: shutil.which('agy' if name == 'antigravity' else name) or fallback
             for name, fallback in PROVIDERS.items()}
# Allowlist, applied again after the user's interactive shell initialization.
ENV_KEYS = {'HOME', 'USER', 'LOGNAME', 'PATH', 'TERM', 'COLORTERM', 'LANG', 'LC_ALL', 'LC_CTYPE',
            'DISPLAY', 'WAYLAND_DISPLAY', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS',
            'XDG_SESSION_TYPE', 'XDG_CURRENT_DESKTOP', 'XAUTHORITY', 'TMUX', 'TMUX_PANE'}

def clean_env(source=None):
    env = {k: v for k, v in (os.environ if source is None else source).items() if k in ENV_KEYS}
    env['PATH'] = f'{HOME}/bin:{HOME}/.local/bin:{HOME}/.local/share/npm-global/bin:/usr/local/bin:/usr/bin:/bin'
    return env

def load(path):
    data = yaml.safe_load(Path(path).read_text())
    jsonschema.Draft202012Validator(json.loads((ROOT/'schemas/workbench.schema.json').read_text())).validate(data)
    seen = set()
    for c in data['contexts']:
        if c['id'] in seen:
            raise ValueError('Duplicate context ID: '+c['id'])
        seen.add(c['id'])
        if any(ord(ch) < 32 for ch in c['title']):
            raise ValueError('Control character in title')
        for key in ['cwd', 'additional_cwds']:
            values = c[key] if isinstance(c[key], list) else [c[key]]
            for value in filter(None, values):
                if '\x00' in value or '\n' in value:
                    raise ValueError('Invalid path')
        if c['enabled'] and not c['cwd']:
            raise ValueError('Enabled context requires cwd')
        if c['risk'] == 'production' and c['enabled'] and c['provider'] != 'codex':
            raise ValueError('Enabled production contexts require the Codex adapter')
    return data

def cwd(c):
    return Path(os.path.expandvars(c['cwd'])).expanduser().resolve()

def directories(c):
    return [cwd(c)] + [Path(os.path.expandvars(p)).expanduser().resolve() for p in c['additional_cwds']]

def secure_dir(path):
    if path != STATE and STATE in path.parents:
        secure_dir(STATE)
    if path.is_symlink():
        raise ValueError('State directory must not be a symlink')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError('State directory must be owned by you with mode 0700')

def atomic(path, value):
    secure_dir(path.parent)
    temp = path.with_name('.'+uuid.uuid4().hex)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream)
    os.replace(temp, path)

def read_state(path):
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError('Unsafe runtime state file')
    return json.loads(path.read_text())

@contextlib.contextmanager
def lock(name, blocking=True):
    secure_dir(STATE)
    fd = os.open(STATE/(name+'.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield
    finally:
        os.close(fd)

def run(args, **kwargs):
    return subprocess.run(args, env=clean_env(), text=True, **kwargs)

class TmuxUnknown(ValueError):
    """No mutation may be inferred from this failed/uncertain observation."""
    def __init__(self, reason):
        self.reason = reason
        super().__init__('tmux state unknown: '+reason+'; preserve sessions and retry inspection')


def tmux_command(*args):
    socket = ['-S', str(TMUX_SOCKET)] if TMUX_SOCKET is not None else ['-L', SERVER]
    return ['/usr/bin/tmux', '-f', str(ROOT/'config/tmux.conf'), *socket, *args]


def tmux(*args, check=True):
    try:
        p = run(tmux_command(*args), capture_output=True, timeout=TMUX_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise TmuxUnknown('timeout') from None
    except (OSError, UnicodeError):
        raise TmuxUnknown('transport_error') from None
    if check and p.returncode:
        raise TmuxUnknown('command_failed')
    return p


def probe(*args, allow_missing_server=False):
    result = tmux(*args, check=False)
    if result.returncode:
        error = result.stderr.strip()
        # Only a missing socket is confirmed absence. Refused connections and a
        # stale socket are uncertain: a server may be starting or recovering.
        if allow_missing_server and not result.stdout and error == 'no sessions':
            return None
        match = re.fullmatch(r'error connecting to (.+) \(No such file or directory\)', error)
        if allow_missing_server and not result.stdout and match:
            socket = Path(match[1])
            if (TMUX_SOCKET is not None and socket != Path(TMUX_SOCKET)) or (TMUX_SOCKET is None and socket.name != SERVER):
                raise TmuxUnknown('unexpected_socket')
            try:
                socket.lstat()
            except FileNotFoundError:
                return None
            except OSError:
                pass
        reason = 'protocol_mismatch' if 'protocol version mismatch' in error else 'probe_failed'
        raise TmuxUnknown(reason)
    if result.stderr.strip():
        raise TmuxUnknown('unexpected_diagnostics')
    return result.stdout


def session(c):
    return 'sfwb-'+c['id']


def target(c):
    output = probe('list-sessions', '-F', '#{session_name}|#{session_id}', allow_missing_server=True)
    if output is None:
        return None
    identities, names = set(), set()
    found = None
    if not output.strip():
        raise TmuxUnknown('empty_session_list')
    for line in output.splitlines():
        parts = line.split('|')
        if len(parts) != 2 or not parts[0] or not re.fullmatch(r'\$[0-9]+', parts[1]):
            raise TmuxUnknown('malformed_session_list')
        name, identity = parts
        if name in names or identity in identities:
            raise TmuxUnknown('ambiguous_session_list')
        names.add(name); identities.add(identity)
        if name == session(c):
            found = identity
    return found


def required_target(c):
    identity = target(c)
    if identity is None:
        raise TmuxUnknown('session_disappeared')
    return identity


def live(c):
    identity = target(c)
    if identity is None:
        return None  # Confirmed absence only; unknown observations raise above.
    output = probe('display-message', '-p', '-t', identity,
                   '#{session_attached}|#{pane_dead}|#{@sfwb_binding}|#{pane_pid}')
    parts = output.strip().split('|')
    if (len(parts) != 4 or not re.fullmatch(r'[0-9]+', parts[0]) or parts[1] not in {'0', '1'}
            or not re.fullmatch(r'[a-f0-9]{64}', parts[2]) or not re.fullmatch(r'[1-9][0-9]*', parts[3])):
        raise TmuxUnknown('malformed_pane_state')
    attached, dead, binding, pane_pid = parts
    if binding != fingerprint(c):
        raise ValueError('Existing session binding changed; do not reattach: '+c['id'])
    return {'attached': int(attached), 'dead': dead == '1', 'pane_pid': int(pane_pid), 'identity': identity}


def provider_pids(c, pane_pid, proc_root=Path('/proc')):
    # Inspect only process metadata, never terminal output or provider environments.
    processes = {}
    for path in proc_root.iterdir():
        if not path.name.isdigit():
            continue
        try:
            if path.stat().st_uid != os.getuid():
                continue
            fields = (path/'stat').read_text().rsplit(')', 1)[1].split()
            if fields[0] == 'Z':
                continue
            processes[int(path.name)] = (int(fields[1]), (path/'comm').read_text().strip())
        except (OSError, ValueError, IndexError):
            continue
    family = {pane_pid}
    while True:
        expanded = family | {pid for pid, (parent, _) in processes.items() if parent in family}
        if expanded == family:
            break
        family = expanded
    expected = {'antigravity': 'agy'}.get(c['provider'], c['provider'])
    return sorted(pid for pid in family if pid in processes and processes[pid][1] == expected)


def wait_provider(c, timeout=40):
    deadline = time.monotonic()+timeout
    previous = None
    while True:
        state = live(c)
        if not state or state['dead']:
            raise ValueError(c['id']+': provider exited during startup; inspect its pane, fix the reported error, then run ai-workbench up '+c['id'])
        pids = provider_pids(c, state['pane_pid'])
        if pids and pids == previous:
            return state
        if time.monotonic() >= deadline:
            raise ValueError(c['id']+': no stable provider process observed; pane preserved. Inspect/attach this context before retrying; no live process was killed')
        previous = pids
        time.sleep(.5)


def set_title(c):
    try:
        run([str(HOME/'bin/ren.sh'), c['title']], check=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        print(c['id']+': title helper unavailable; continuing provider startup/attachment', file=sys.stderr)


def retry_start(c, operation):
    # Only early unsuccessful exits. Normal exits, signals and established sessions
    # are never restarted. Each Codex attempt revalidates its exact saved binding.
    for attempt in range(3):
        started = time.monotonic()
        code = operation()
        elapsed = time.monotonic()-started
        if not code:
            return
        if attempt == 2 or elapsed >= 10 or code < 0 or code in {126, 127, 130, 143}:
            raise ValueError(c['id']+': provider exited with status '+str(code)+'; inspect this pane, then retry with ai-workbench up '+c['id'])
        print(c['id']+': early provider failure; retry '+str(attempt+2)+'/3', flush=True)
        time.sleep(attempt+1)


def fingerprint(c):
    return hashlib.sha256(json.dumps(c, sort_keys=True).encode()).hexdigest()

def validate_context(c):
    if not c['enabled']:
        raise ValueError(c['id']+' disabled: '+c.get('disabled_reason', 'not enabled'))
    for p in directories(c):
        if not p.is_dir():
            raise ValueError('Missing directory: '+str(p))
    if not Path(PROVIDERS[c['provider']]).is_file():
        raise ValueError('Missing provider: '+c['provider'])

def bridge(pid, action='check', **kw):
    p = run(['/usr/bin/python3', str(Path(__file__).with_name('desktop.py')),
             json.dumps({'pid': pid, 'action': action, **kw})], capture_output=True)
    if p.returncode:
        raise ValueError('Owned terminal unavailable: '+p.stderr.strip())
    return json.loads(p.stdout)

def birth(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError):
        return None

def open_tab(c, manifest):
    command = [str(SELF), '--manifest', str(manifest), '_attach', c['id']]
    path = STATE/'terminal.json'
    owner = read_state(path)
    if owner and birth(owner['pid']) == owner['birth']:
        # Address the exact standalone process, never the active desktop application.
        bridge(owner['pid'], 'tab', command=shlex.join(command), title=c['title'], cwd=str(cwd(c)))
    else:
        if not (os.environ.get('WAYLAND_DISPLAY') or os.environ.get('DISPLAY')):
            raise ValueError('No graphical session; use attach from a terminal')
        terminal_config = STATE/'terminal-config'
        secure_dir(terminal_config)
        terminal_env = clean_env()
        prepared = bridge(0, 'prepare', config=str(terminal_config), command=shlex.join(command),
                          columns=197, rows=49)
        terminal_env['XDG_CONFIG_HOME'] = str(terminal_config)
        terminal_env['GSETTINGS_BACKEND'] = 'keyfile'
        terminal_env.pop('TMUX', None)
        terminal_env.pop('TMUX_PANE', None)
        p = subprocess.Popen(['/usr/bin/ptyxis', '--standalone', '--new-window', '--gapplication-app-id=org.starforge.AIWorkbench',
                              '--tab-with-profile='+prepared['profile'],
                              '--title='+c['title'], '--working-directory='+str(cwd(c))],
                             env=terminal_env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        atomic(path, {'pid': p.pid, 'birth': birth(p.pid)})
        for _ in range(50):
            try:
                bridge(p.pid)
                break
            except ValueError:
                if p.poll() is not None:
                    raise ValueError('Ptyxis exited before registration')
                time.sleep(.1)
        else:
            raise ValueError('Ptyxis registration timed out; inspect before retry')
    # Parent holds the global lock through attachment acknowledgment. Duplicate requests wait.
    for _ in range(60):
        state = live(c)
        if state and state['attached']:
            return
        time.sleep(.1)
    raise ValueError('Tab attachment unconfirmed; pending receipt retained, inspect status and attach manually')

# Match established terminal labels, never infer a conversation from its subject.
ALIASES = {
    'local-sysadmin': ['Local sys admin', 'loal sys admin'],
    'aws-infrastructure': ['AWS Infrastructure', 'AWS infra'],
    'azure-infrastructure': ['Azure Infrastructure'],
    'claude-code': ['Claude Code'],
}

def managed_ttys():
    output = probe('list-panes', '-a', '-F', '#{pane_dead}|#{pane_tty}', allow_missing_server=True)
    if output is None:
        return set()
    lines = output.splitlines()
    if not lines:
        raise TmuxUnknown('malformed_pane_list')
    ttys = set()
    for line in lines:
        fields = line.split('|')
        if len(fields) != 2 or fields[0] not in {'0', '1'}:
            raise TmuxUnknown('malformed_pane_list')
        dead, tty = fields
        if dead == '1' and not tty:
            continue  # A retained, exited pane can have no terminal anymore.
        if not re.fullmatch(r'/dev/(?:pts/[0-9]+|tty[^/\s]+)', tty):
            raise TmuxUnknown('malformed_pane_list')
        ttys.add(tty)
    return ttys

def external_session(c, proc_root=Path('/proc')):
    labels = {c['title'].casefold(), *[s.casefold() for s in ALIASES.get(c['id'], [])]}
    matches = []
    owned_ttys = managed_ttys()
    for proc in proc_root.iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if proc.stat().st_uid != os.getuid() or (proc/'comm').read_text().strip() != {'antigravity': 'agy'}.get(c['provider'], c['provider']):
                continue
            tty = os.readlink(proc/'fd/0')
            if not tty.startswith('/dev/pts/') or tty in owned_ttys:
                continue
            label = (HOME/'.cache/terminal-titles'/tty.replace('/', '_')).read_text().strip()
            if label.casefold() not in labels:
                continue
            args = (proc/'cmdline').read_bytes().decode().split('\0')
            ids = [a for a in args if re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', a)]
            matches.append({'pid': int(proc.name), 'birth': birth(int(proc.name)), 'title': label,
                            'cwd': str((proc/'cwd').resolve()), 'id': ids[0] if len(ids) == 1 else None})
        except (OSError, UnicodeError):
            continue
    if len(matches) > 1:
        raise ValueError('Multiple live sessions match '+c['id']+'; select the original tab manually')
    return matches[0] if matches else None

def reuse_external(c, existing, headless=False):
    if birth(existing['pid']) != existing['birth']:
        raise ValueError('Existing session changed; retry discovery')
    if not headless:
        bridge(0, 'focus', title=existing['title'])
    print(c['id']+': existing live tab '+existing['title']+' (cwd '+existing['cwd']+')')


def up(c, manifest, headless=False):
    with lock('launch'):
        state = live(c)
        # A live managed pane is authoritative, even when its terminal window closed.
        if not state or state['dead']:
            existing = external_session(c)
            if existing:
                reuse_external(c, existing, headless)
                return
        validate_context(c)
        if (not state or state['dead']) and c['resume_policy'] != 'never':
            saved_session(c)  # Fail before opening a tab when its binding needs repair.
        if not state:
            args = [str(SELF), '--manifest', str(manifest), '_menu', c['id']]
            tmux('new-session', '-d', '-s', session(c), '-c', str(cwd(c)), *args)
            identity = required_target(c)
            tmux('set-option', '-t', identity, '@sfwb_binding', fingerprint(c))
            tmux('set-option', '-t', identity, 'remain-on-exit', 'on')
            tmux('set-option', '-t', identity, 'set-titles', 'on')
            tmux('set-option', '-t', identity, 'set-titles-string', c['title'].replace('#', '##'))
            state = live(c)
        elif state['dead']:
            tmux('respawn-pane', '-t', state.get('identity') or required_target(c), '-c', str(cwd(c)),
                 str(SELF), '--manifest', str(manifest), '_menu', c['id'])
            state = live(c)
        if not state:
            raise ValueError(c['id']+': tmux pane disappeared during startup; inspect launcher/provider errors before retrying')
        if state['attached'] or headless:
            wait_provider(c)
            print(c['id']+': provider process present; '+('reused' if state['attached'] else 'detached'))
            return
        pending = STATE/(c['id']+'-pending.json')
        if pending.exists():
            raise ValueError('Unconfirmed prior tab request; use attach in an existing terminal, not another up')
        atomic(pending, {'context': c['id'], 'time': time.time()})
        open_tab(c, manifest)
        wait_provider(c)
        pending.unlink()
        print(c['id']+': attached; provider process present')

def attach(c):
    validate_context(c)
    if not sys.stdin.isatty():
        raise ValueError('attach requires an interactive terminal')
    with lock('attach-'+c['id'], blocking=False):
        state = live(c)
        if not state or state['dead']:
            raise ValueError('No live session; use up first')
        if state['attached']:
            print('Already attached; switch to the existing tab')
            return
        set_title(c)
        # Keep per-context flock held for the client lifetime; never detach another active client.
        run(tmux_command('attach-session', '-t', state.get('identity') or required_target(c)), check=True)
    live(c)  # Do not clear a pending receipt if the final observation is uncertain.
    pending = STATE/(c['id']+'-pending.json')
    if pending.exists():
        pending.unlink()

def checkout_keys(c):
    keys = []
    for p in directories(c):
        out = run(['git', '-C', str(p), 'rev-parse', '--show-toplevel'], capture_output=True)
        if out.returncode == 0:
            keys.append(str(Path(out.stdout.strip()).resolve()))
    return sorted(set(keys))

def reject_external_agents(keys):
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit() or int(proc.name) == os.getpid():
            continue
        try:
            if proc.stat().st_uid != os.getuid():
                continue
            name = (proc/'comm').read_text().strip().lower()
            if name not in {'codex', 'claude', 'agy', 'opencode'}:
                continue
            active = (proc/'cwd').resolve()
            if any(active == Path(k) or Path(k) in active.parents for k in keys):
                raise ValueError('Another provider process is active in this checkout; use a separate worktree')
        except (PermissionError, FileNotFoundError, ProcessLookupError):
            continue


def codex_sessions():
    sessions = {}
    for db in (HOME/'.codex').glob('state*.sqlite'):
        with sqlite3.connect('file:'+str(db)+'?mode=ro', uri=True) as conn:
            for identity, directory in conn.execute("select id,cwd from threads where archived=0 and source='cli'"):
                sessions[identity] = str(Path(directory).resolve())
    return sessions

def provider_sessions(c):
    if c['provider'] == 'codex':
        return codex_sessions()
    if c['provider'] != 'opencode':
        raise ValueError('Provider has no verified local session catalog')
    db = HOME/'.local/share/opencode/opencode.db'
    if not db.exists():
        return {}
    with sqlite3.connect(db.as_uri()+'?mode=ro', uri=True, timeout=2) as conn:
        return {identity: str(Path(directory).resolve()) for identity, directory in conn.execute(
            'select id,directory from session where time_archived is null and parent_id is null')}


def bind_session(c, identity):
    live(c)  # Catalog validation alone cannot establish tmux safety.
    source = str(cwd(c))
    if c['provider'] in {'codex', 'opencode'}:
        source = provider_sessions(c).get(identity)
        if source is None:
            raise ValueError('Provider session does not exist or is archived')
    data = {'id':identity, 'provider':c['provider'], 'cwd':str(cwd(c)), 'source_cwd':source}
    atomic(STATE/'sessions'/(c['id']+'.json'), data)
    return data

def saved_session(c):
    data = read_state(STATE/'sessions'/(c['id']+'.json'))
    if data and (data.get('provider') != c['provider'] or data.get('cwd') != str(cwd(c))):
        raise ValueError(c['id']+': saved session provider/cwd mismatch; review the directory change and rebind the existing ID with bind')
    if data and c['provider'] in {'codex', 'opencode'}:
        actual = provider_sessions(c).get(data['id'])
        if actual not in {data.get('source_cwd', data['cwd']), data['cwd']}:
            raise ValueError('Saved provider session is missing or its directory changed unexpectedly')
    return data

def remember_created(c, before):
    candidates = [identity for identity, directory in provider_sessions(c).items()
                  if identity not in before and directory == str(cwd(c))]
    if len(candidates) == 1:
        bind_session(c, candidates[0])
        return True
    if len(candidates) > 1:
        raise ValueError('More than one new conversation appeared; refusing to guess the binding')
    return False

def start_codex(c):
    existing = external_session(c)
    if existing:
        reuse_external(c, existing, headless=True)
        return
    validate_context(c)
    with contextlib.ExitStack() as stack:
        stack.enter_context(lock('provider-'+c['id'], blocking=False))
        data = saved_session(c)
        # The same UUID may still be running in a terminal with a different label.
        if data:
            for proc in Path('/proc').iterdir():
                try:
                    if proc.name.isdigit() and (proc/'comm').read_text().strip() == c['provider']:
                        args = (proc/'cmdline').read_bytes().decode().split('\0')
                        if data['id'] in args:
                            raise ValueError('This conversation is already running; use its existing terminal')
                except (OSError, UnicodeError):
                    continue
        keys = checkout_keys(c)
        reject_external_agents(keys)
        for key in keys:
            stack.enter_context(lock('checkout-'+hashlib.sha256(key.encode()).hexdigest(), blocking=False))
        before = provider_sessions(c) if not data else None
        stop = threading.Event()
        errors = []
        def remember():
            while not stop.wait(.3):
                try:
                    if remember_created(c, before):
                        return
                except (ValueError, OSError, sqlite3.Error) as exc:
                    errors.append(str(exc)); return
        watcher = threading.Thread(target=remember, daemon=True) if not data else None
        if watcher:
            watcher.start()
        try:
            result = run(provider_argv(c, 'resume' if data else 'new'), cwd=cwd(c))
        finally:
            stop.set()
            if watcher:
                watcher.join()
                if not errors and not saved_session(c):
                    remember_created(c, before)
        if errors:
            raise ValueError('Conversation binding needs attention: '+errors[0])
        if result.returncode:
            print('Codex exited with status', result.returncode)
        return result.returncode


def provider_argv(c, choice):
    exe = PROVIDERS[c['provider']]
    if c['resume_policy'] == 'never' and choice != 'new':
        raise ValueError('This context does not permit conversation resume')
    if c['provider'] == 'codex':
        base = [exe, '-c', 'check_for_update_on_startup=false', '-C', str(cwd(c)), '-s', 'read-only' if c['risk'] == 'cloud-infrastructure' else 'workspace-write', '-a', 'on-request']
        for p in c['additional_cwds']:
            base += ['--add-dir', str(Path(p).expanduser().resolve())]
        if choice == 'picker':
            return base+['resume', '--all']
        if choice == 'resume':
            if c['resume_policy'] == 'last-in-directory':
                return base+['resume', '--last']
            data = saved_session(c)
            if not data:
                return base+['resume', '--all']
            return base+['resume', data['id']]
        return base
    if c['provider'] == 'opencode':
        base = [exe, str(cwd(c)), '--hostname', '127.0.0.1']
        if choice == 'new':
            return base
        if choice == 'resume':
            data = saved_session(c)
            if not data:
                raise ValueError('Bind an exact OpenCode session before resuming')
            return base+['--session', data['id']]
        raise ValueError('OpenCode picker/last-session selection is not supported; bind an exact session')
    if c['provider'] == 'claude':
        if choice == 'resume':
            data = saved_session(c)
            return [exe, '--permission-mode', 'default', '--resume'] + ([data['id']] if data else [])
        return [exe, '--permission-mode', 'default']+(['--resume'] if choice == 'picker' else [])
    if c['provider'] == 'antigravity':
        if choice == 'resume':
            data = saved_session(c)
            if not data:
                raise ValueError('Bind an explicitly selected conversation ID first')
            return [exe, '--sandbox', '--mode', 'plan', '--conversation', data['id']]
        if choice == 'picker':
            raise ValueError('agy has no verified picker; choose new or bind an explicit conversation')
        return [exe, '--sandbox', '--mode', 'plan']
    if c['provider'] == 'ollama' and choice == 'new':
        return [exe, 'run', 'qwen3:8b']
    raise ValueError('Unsupported provider operation')

def menu(c):
    # Drop inherited shell credentials before displaying the menu or spawning any child.
    sanitized = clean_env()
    os.environ.clear()
    os.environ.update(sanitized)
    set_title(c)
    if c['provider'] in {'codex', 'opencode'}:
        retry_start(c, lambda: start_codex(c))
        return
    if c['provider'] == 'ollama':
        retry_start(c, lambda: run(provider_argv(c, 'new'), cwd=cwd(c)).returncode)
        return
    with contextlib.ExitStack() as stack:
        stack.enter_context(lock('provider-'+c['id'], blocking=False))
        keys = checkout_keys(c)
        reject_external_agents(keys)
        for key in keys:
            stack.enter_context(lock('checkout-'+hashlib.sha256(key.encode()).hexdigest(), blocking=False))
        data = saved_session(c) if c['resume_policy'] != 'never' else None
        choice = 'resume' if data else ('picker' if c['provider'] == 'claude' and c['resume_policy'] == 'picker' else 'new')
        retry_start(c, lambda: run(provider_argv(c, choice), cwd=cwd(c)).returncode)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest', type=Path, default=(ROOT/'config/workbench.yaml' if (ROOT/'config/workbench.yaml').exists() else ROOT/'config/workbench.example.yaml'))
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('command', choices=['doctor','list','plan','up','attach','status','bind','_attach','_menu'])
    p.add_argument('contexts', nargs='*')
    p.add_argument('--headless', action='store_true', help='Create/reuse detached menu sessions without windows')
    p.add_argument('--session-id')
    args = p.parse_args(argv)
    manifest = args.manifest.resolve()
    data = load(manifest)
    byid = {c['id']: c for c in data['contexts']}
    selected = [byid[k] for k in args.contexts] if args.contexts else list(byid.values())
    if args.command in ['attach','_attach','_menu','bind'] and len(args.contexts) != 1:
        raise ValueError('Exactly one context ID required')
    if args.dry_run or args.command in ['list','plan']:
        print(json.dumps([{'id':c['id'],'title':c['title'],'cwd':str(cwd(c)) if c['cwd'] else None,
                           'additional_cwds':c['additional_cwds'],'provider':c['provider'], 'resume_policy':c['resume_policy'],
                           'enabled':c['enabled'],'action':('resume saved conversation or create and remember' if c['provider'] == 'codex' else ('start interactive qwen3:8b' if c['provider'] == 'ollama' else 'resume saved conversation or open provider directly')) if c['enabled'] else 'disabled'} for c in selected], indent=2))
        return
    if args.command == 'doctor':
        problems = []
        for cmd in ['tmux','ptyxis','zsh']:
            if not shutil.which(cmd):
                problems.append('Missing '+cmd)
        for c in selected:
            try:
                state = live(c)
                print(c['id']+': tmux '+('present' if state else 'absent'))
                validate_context(c)
                if c['resume_policy'] != 'never':
                    saved_session(c)
                print(c['id']+': available')
            except ValueError as exc:
                print(str(exc))
                if c['enabled']:
                    problems.append(str(exc))
        if problems:
            raise ValueError('; '.join(problems))
        print('Manual-use acceptance pending: 7 days, reboot and logout/login. No autostart.')
    elif args.command == 'status':
        failures = []
        for c in selected:
            try:
                state = live(c)
                pids = provider_pids(c, state['pane_pid']) if state and not state['dead'] else []
                print(c['id'], json.dumps({'probe': 'present' if state else 'absent', 'external': external_session(c), 'managed': state, 'provider_pids': pids, 'binding': saved_session(c)}))
            except (ValueError, OSError) as exc:
                failures.append(c['id'])
                print(c['id'], json.dumps({'probe': 'unknown', 'error': str(exc)}))
        if failures:
            raise ValueError('Contexts need attention: '+', '.join(failures))
    elif args.command == 'up':
        failures = []
        for c in selected:
            try:
                if not c['enabled'] and not args.contexts and not external_session(c):
                    print(c['id']+': skipped (disabled)')
                    continue
                up(c, manifest, args.headless)
            except (ValueError, OSError) as exc:
                failures.append(str(exc)); print(str(exc), file=sys.stderr)
        if failures:
            raise ValueError('Some contexts could not open; see errors above')
    elif args.command in ['attach','_attach']:
        attach(selected[0])
    elif args.command == '_menu':
        menu(selected[0])
    elif args.command == 'bind':
        c = selected[0]
        validate_context(c)
        if not args.session_id or not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', args.session_id):
            raise ValueError('Provide an explicit session ID via --session-id')
        bind_session(c, args.session_id)
        print('Saved verified conversation binding')

if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, jsonschema.ValidationError, sqlite3.Error) as exc:
        print('Workbench error: '+str(exc).splitlines()[0], file=sys.stderr)
        sys.exit(2)
