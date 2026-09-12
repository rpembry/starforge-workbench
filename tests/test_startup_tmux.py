"""Optional real tmux test; uses only a private server and synthetic provider."""
import copy
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get('WB_TEST_TMUX') == '1' and shutil.which('tmux'), 'Set WB_TEST_TMUX=1 for isolated tmux integration')
class StartupIntegration(unittest.TestCase):
    def test_transient_failure_recovery_and_repeat_up_preserves_provider(self):
        with tempfile.TemporaryDirectory(prefix='wb-startup-fixture-') as temp:
            folder = Path(temp)
            server = 'wb-fixture-'+uuid.uuid4().hex
            module_path = ROOT/'src/starforge_workbench/cli.py'
            spec = importlib.util.spec_from_file_location('fixture_launcher', module_path)
            cli = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cli)
            cli.STATE = folder/'state'
            cli.SERVER = server
            cli.TMUX_SOCKET = folder/'fixture.sock'
            original_run = cli.run
            def isolated_run(args, **kwargs):
                if args[0] == '/usr/bin/tmux':
                    self.assertIn('-S', args)
                    self.assertEqual(args[args.index('-S')+1], str(folder/'fixture.sock'))
                    self.assertNotIn('-L', args)
                return original_run(args, **kwargs)
            cli.run = isolated_run
            cli.HOME = folder
            fixture = folder/'provider'
            fixture.write_text('#!'+sys.executable+'\n'+f'''
import ctypes, time
from pathlib import Path
p = Path({str(folder/'attempts')!r})
n = int(p.read_text())+1 if p.exists() else 1
p.write_text(str(n))
if n < 3: raise SystemExit(1)
ctypes.CDLL(None).prctl(15, b'ollama', 0, 0, 0)
time.sleep(60)
''')
            fixture.chmod(0o700)
            cli.PROVIDERS = {**cli.PROVIDERS, 'ollama': str(fixture)}
            runner = folder/'runner'
            runner.write_text('#!'+sys.executable+'\n'+f'''
import importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('fixture_launcher', {str(module_path)!r})
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.STATE = Path({str(cli.STATE)!r})
m.HOME = Path({str(folder)!r})
m.SERVER = {server!r}
m.TMUX_SOCKET = Path({str(folder/'fixture.sock')!r})
m.SELF = Path({str(runner)!r})
m.PROVIDERS['ollama'] = {str(fixture)!r}
m.main(sys.argv[1:])
''')
            runner.chmod(0o700)
            cli.SELF = runner
            data = yaml.safe_load((ROOT/'config/workbench.example.yaml').read_text())
            c = copy.deepcopy(next(c for c in data['contexts'] if c['provider'] == 'ollama'))
            c.update(cwd=str(folder), additional_cwds=[], title='Isolated startup fixture', resume_policy='never')
            data['contexts'] = [c]
            manifest = folder/'manifest.yaml'
            manifest.write_text(yaml.safe_dump(data))
            try:
                self.assertIsNone(cli.target(c))
                cli.up(c, manifest, headless=True)
                before = cli.live(c)
                pids = cli.provider_pids(c, before['pane_pid'])
                self.assertTrue(pids)
                self.assertEqual((folder/'attempts').read_text(), '3')
                cli.up(c, manifest, headless=True)
                self.assertEqual(cli.provider_pids(c, cli.live(c)['pane_pid']), pids)
                self.assertEqual((folder/'attempts').read_text(), '3')
                with patch.object(cli, 'probe', side_effect=cli.TmuxUnknown('timeout')):
                    with self.assertRaises(cli.TmuxUnknown):
                        cli.up(c, manifest, headless=True)
                self.assertEqual(cli.provider_pids(c, cli.live(c)['pane_pid']), pids)
                self.assertEqual((folder/'attempts').read_text(), '3')
            finally:
                assert cli.TMUX_SOCKET == folder/'fixture.sock'
                subprocess.run(['tmux', '-S', str(folder/'fixture.sock'), 'kill-server'], capture_output=True, timeout=3)


if __name__ == '__main__':
    unittest.main()
