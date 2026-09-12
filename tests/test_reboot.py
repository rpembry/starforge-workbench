"""Regression coverage for the September 12 manual post-reboot failures."""
import contextlib
import copy
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_launcher import cli, ROOT


class RebootTests(unittest.TestCase):
    def setUp(self):
        absent = patch.object(cli, 'probe', return_value=None)
        absent.start()
        self.addCleanup(absent.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.contexts = cli.load(ROOT/'config/workbench.example.yaml')['contexts']

    def test_stale_binding_fails_before_respawn_or_tab_request(self):
        c = self.contexts[0]
        with patch.object(cli, 'STATE', self.state), patch.object(cli, 'live', return_value={'dead': True, 'attached': 0}), patch.object(cli, 'external_session', return_value=None), patch.object(cli, 'validate_context'), patch.object(cli, 'saved_session', side_effect=ValueError('stale directory')), patch.object(cli, 'tmux') as tmux, patch.object(cli, 'open_tab') as tab:
            with self.assertRaisesRegex(ValueError, 'stale directory'):
                cli.up(c, ROOT/'config/workbench.example.yaml')
            tmux.assert_not_called()
            tab.assert_not_called()

    def test_status_reports_remaining_contexts_after_bad_binding(self):
        output = io.StringIO()
        with patch.object(cli, 'external_session', return_value=None), patch.object(cli, 'live', return_value=None), patch.object(cli, 'saved_session', side_effect=[ValueError('stale directory'), None]), contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(ValueError, 'ai-workbench'):
                cli.main(['status', 'ai-workbench', 'local-sysadmin'])
        self.assertIn('stale directory', output.getvalue())
        self.assertIn('local-sysadmin', output.getvalue())

    def test_doctor_checks_conversation_binding(self):
        with patch.object(cli, 'validate_context'), patch.object(cli, 'saved_session', side_effect=ValueError('stale directory')), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, 'stale directory'):
                cli.main(['doctor', 'ai-workbench'])

    def test_other_providers_start_without_launcher_input(self):
        for identity, binding, suffix in [
            ('claude-code', {'id': 'exact-session'}, ['--resume', 'exact-session']),
            ('claude-code', None, ['--permission-mode', 'default', '--resume']),
            ('antigravity', None, ['--sandbox', '--mode', 'plan']),
            ('antigravity', {'id': 'exact-session'}, ['--conversation', 'exact-session']),
        ]:
            with self.subTest(identity=identity, binding=binding):
                c = copy.deepcopy(next(c for c in self.contexts if c['id'] == identity))
                c['cwd'] = str(self.state)
                with patch.object(cli, 'STATE', self.state), patch.object(cli, 'checkout_keys', return_value=[]), patch.object(cli, 'reject_external_agents'), patch.object(cli, 'saved_session', return_value=binding), patch.object(cli, 'run', return_value=SimpleNamespace(returncode=0)) as run, patch('builtins.input', side_effect=AssertionError('launcher prompt')), patch.dict(cli.os.environ):
                    cli.menu(c)
                argv = run.call_args.args[0]
                self.assertEqual(argv[-len(suffix):], suffix)
                self.assertEqual(run.call_args.kwargs['cwd'], self.state)
                self.assertNotIn('--prompt', argv)
                self.assertNotIn('--dangerously-skip-permissions', argv)
