"""Opt-in graphical test: isolated server, harmless status menus, guaranteed cleanup."""
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import uuid
import yaml

ROOT = Path(__file__).resolve().parents[1]

def main():
    if Path('/proc/1/comm').read_text().strip() != 'systemd':
        raise RuntimeError('Run this desktop test outside the PID/PTY sandbox; isolated PTY numbers collide with host title caches')
    server = 'sfwb-test-'+uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix='sfwb-test-') as tmp:
        base = Path(tmp)
        fake_ollama = base/'fake-ollama'
        fake_ollama.write_text('#!/bin/sh\nexec /usr/bin/cat\n')
        fake_ollama.chmod(0o700)
        wrapper = base/'runner'
        wrapper.write_text(f'''#!{ROOT}/.venv/bin/python
import sys
from pathlib import Path
sys.path.insert(0, {str(ROOT/'src/starforge_workbench')!r})
import cli
cli.STATE = Path({str(base/'state')!r})
cli.SERVER = {server!r}
cli.PROVIDERS['ollama'] = {str(fake_ollama)!r}
cli.SELF = Path({str(wrapper)!r})
try:
    cli.main()
except (ValueError, OSError) as exc:
    with open({str(base/'errors.txt')!r}, 'a') as log: log.write(str(exc)+'\\n')
    print(str(exc), file=sys.stderr)
    sys.exit(2)
''')
        wrapper.chmod(0o700)
        orig = yaml.safe_load((ROOT/'config/workbench.example.yaml').read_text())
        c = next(c for c in orig['contexts'] if c['id']=='ollama')
        c.update(cwd=tmp, additional_cwds=[])
        data = {'version':1,'autostart':False,'contexts':[dict(c,id='test-a',title='Workbench test A'),dict(c,id='test-b',title='Workbench test B')]}
        manifest = base/'manifest.yaml';manifest.write_text(yaml.safe_dump(data))
        cmd = [str(wrapper),'--manifest',str(manifest)]
        def tmux(*args):
            return subprocess.check_output(['/usr/bin/tmux','-L',server,*args],text=True).strip()
        provider_fixture = None
        try:
            subprocess.run(cmd+['up','test-a'],check=True,timeout=20)
            # Harmless native-name fixture shares the managed pane's terminal.
            # This reproduces a live provider surviving its closed GUI window.
            tty = tmux('display-message','-p','-t','sfwb-test-a','#{pane_tty}')
            with open(tty, 'rb', buffering=0) as terminal:
                provider_fixture = subprocess.Popen(['/usr/bin/python3', '-c',
                    "import ctypes,time; ctypes.CDLL(None).prctl(15,b'ollama',0,0,0); time.sleep(120)"],
                    stdin=terminal, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(50):
                if Path(f'/proc/{provider_fixture.pid}/comm').read_text().strip() == 'ollama': break
                time.sleep(.02)
            else: raise AssertionError('Provider fixture did not initialize')
            children=[subprocess.Popen(cmd+['up','test-b']) for _ in range(3)]
            assert all(p.wait(timeout=25)==0 for p in children)
            first=tmux('list-sessions','-F','#{session_name}:#{session_attached}')
            assert set(first.splitlines())=={'sfwb-test-a:1','sfwb-test-b:1'},first
            subprocess.run(cmd+['up'],check=True,timeout=20)
            assert tmux('list-sessions','-F','#{session_name}:#{session_attached}')==first
            tmux('detach-client','-s','sfwb-test-a')
            subprocess.run(cmd+['up','test-a'],check=True,timeout=20)
            assert tmux('display-message','-p','-t','sfwb-test-a','#{session_attached}')=='1'
            actual=tmux('display-message','-p','-t','sfwb-test-a','#{pane_current_path}')
            assert actual==tmp,(actual,tmp)
            pane_pids = tmux('list-panes','-a','-F','#{pane_pid}')
            owner = json.loads((base/'state/terminal.json').read_text())
            geometry = json.loads(subprocess.check_output(['/usr/bin/python3', str(ROOT/'src/starforge_workbench/desktop.py'), json.dumps({'action':'geometry','pid':owner['pid']})], text=True))
            print('Measured geometry:', geometry)
            assert geometry == {'width':1588,'height':979}, geometry
            os.kill(owner['pid'], signal.SIGTERM)
            for _ in range(50):
                if all(line.endswith(':0') for line in tmux('list-sessions','-F','#{session_name}:#{session_attached}').splitlines()):
                    break
                time.sleep(.1)
            else: raise AssertionError('Clients remained attached after owned window closed')
            subprocess.run(cmd+['up'],check=True,timeout=25)
            assert tmux('list-sessions','-F','#{session_name}:#{session_attached}') == first
            assert tmux('list-panes','-a','-F','#{pane_pid}') == pane_pids
            print('PASS: isolated visible-tab routing; concurrent/repeated up; detach/reattach; window close/reopen preserves pane PIDs and cwd. No model loaded.')
        except Exception:
            out = subprocess.run(['/usr/bin/tmux','-L',server,'capture-pane','-p','-t','sfwb-test-a'],capture_output=True,text=True)
            print('Fixture pane diagnostic:',out.stdout)
            if (base/'errors.txt').exists(): print('Attach errors:',(base/'errors.txt').read_text())
            raise
        finally:
            if provider_fixture is not None:
                provider_fixture.terminate()
                provider_fixture.wait(timeout=5)
            subprocess.run(['/usr/bin/tmux','-L',server,'kill-server'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            state=base/'state/terminal.json'
            if state.exists():
                owner=json.loads(state.read_text())
                try:
                    stat=Path(f'/proc/{owner["pid"]}/stat').read_text().rsplit(')',1)[1].split()[19]
                    if stat==owner['birth']:os.kill(owner['pid'],signal.SIGTERM)
                except (FileNotFoundError,ProcessLookupError):pass

if __name__=='__main__':main()
