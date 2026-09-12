import copy
import contextlib
import io
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import jsonschema
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('launcher', ROOT/'src/starforge_workbench/cli.py')
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)

class LauncherTests(unittest.TestCase):
    def setUp(self):
        absent = patch.object(cli, 'probe', return_value=None)
        absent.start()
        self.addCleanup(absent.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.data = yaml.safe_load((ROOT/'config/workbench.example.yaml').read_text())
        self.c = copy.deepcopy(self.data['contexts'][0])
        self.c['cwd'] = str(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def load(self):
        p = self.path/'manifest.yaml'
        p.write_text(yaml.safe_dump(self.data))
        return cli.load(p)

    def test_example_contexts_cover_supported_providers(self):
        data = self.load()
        self.assertEqual(len(data['contexts']), 6)
        self.assertEqual({c['provider'] for c in data['contexts']}, {'codex', 'claude', 'opencode', 'antigravity', 'ollama'})

    def test_duplicate_ids_rejected(self):
        self.data['contexts'].append(self.data['contexts'][0])
        with self.assertRaises(ValueError): self.load()

    def test_autostart_and_unknown_keys_rejected(self):
        self.data['autostart'] = True
        with self.assertRaises(jsonschema.ValidationError): self.load()
        self.data['autostart'] = False
        self.data['shell_command'] = 'unreviewed'
        with self.assertRaises(jsonschema.ValidationError): self.load()

    def test_enabled_production_uses_workspace_write_and_missing_cwd_rejected(self):
        self.data['contexts'][0]['risk'] = 'production'
        production = self.load()['contexts'][0]
        args = cli.provider_argv(production, 'new')
        self.assertEqual(args[args.index('-s')+1], 'workspace-write')
        self.assertEqual(args[-2:], ['-a', 'on-request'])
        self.data['contexts'][0]['provider'] = 'claude'
        with self.assertRaises(ValueError): self.load()
        self.data['contexts'][0]['provider'] = 'codex'
        self.data['contexts'][0]['risk'] = 'local'
        self.data['contexts'][0]['cwd'] = None
        with self.assertRaises(ValueError): self.load()

    def test_final_environment_allowlist(self):
        env = cli.clean_env({'HOME': '/home/example', 'TERM':'xterm', 'MYSQL_PASSWORD':'synthetic',
                             'AWS_PROFILE':'synthetic', 'GH_TOKEN':'synthetic', 'BASH_ENV':'evil',
                             'LD_PRELOAD':'evil', 'PYTHONPATH':'evil', 'RESTIC_PASSWORD':'synthetic'})
        self.assertEqual(set(env), {'HOME','TERM','PATH'})

    def test_dry_run_never_launches_or_creates_state(self):
        with patch.object(cli, 'STATE', self.path/'absent'), patch.object(cli.subprocess, 'run', side_effect=AssertionError('spawned')):
            with contextlib.redirect_stdout(io.StringIO()):
                cli.main(['--dry-run','up'])
        self.assertFalse((self.path/'absent').exists())

    def test_provider_has_no_initial_prompt(self):
        args = cli.provider_argv(self.c, 'new')
        self.assertEqual(args[-2:], ['-a','on-request'])
        self.c['risk'] = 'cloud-infrastructure'
        self.assertIn('read-only', cli.provider_argv(self.c, 'new'))

    def test_session_provider_cwd_mismatch(self):
        with patch.object(cli,'STATE',self.path):
            cli.atomic(self.path/'sessions'/f'{self.c["id"]}.json', {'provider':'claude','cwd':str(self.path),'id':'synthetic'})
            with self.assertRaises(ValueError): cli.saved_session(self.c)

    def test_binding_before_first_launch_creates_private_root(self):
        state = self.path/'new-state'
        with patch.object(cli,'STATE',state):
            cli.atomic(state/'sessions'/'test.json', {'id':'synthetic'})
            self.assertEqual(state.stat().st_mode & 0o777, 0o700)
            self.assertEqual((state/'sessions/test.json').stat().st_mode & 0o777, 0o600)

    def test_unsafe_state_symlink_rejected(self):
        target = self.path/'target';target.write_text('{}')
        link = self.path/'link';link.symlink_to(target)
        with self.assertRaises(ValueError): cli.read_state(link)

    def test_duplicate_up_reuses_attached_session(self):
        with patch.object(cli,'STATE',self.path), patch.object(cli,'validate_context'), patch.object(cli,'live',return_value={'attached':1,'dead':False}), patch.object(cli,'tmux',side_effect=AssertionError('duplicate')), patch.object(cli,'open_tab',side_effect=AssertionError('duplicate tab')), patch.object(cli, 'wait_provider'):
            cli.up(self.c, ROOT/'config/workbench.example.yaml')
            cli.up(self.c, ROOT/'config/workbench.example.yaml')

    def test_pending_tab_fails_closed(self):
        (self.path/(self.c['id']+'-pending.json')).write_text('{}')
        with patch.object(cli,'STATE',self.path), patch.object(cli,'validate_context'), patch.object(cli,'live',return_value={'attached':0,'dead':False}), patch.object(cli,'open_tab',side_effect=AssertionError('duplicate tab')), patch.object(cli, 'wait_provider'):
            with self.assertRaises(ValueError): cli.up(self.c, ROOT/'config/workbench.example.yaml')

    def test_never_resume_and_ollama_8b_start(self):
        self.c['resume_policy']='never'
        with self.assertRaises(ValueError):cli.provider_argv(self.c,'resume')
        self.c['provider']='ollama'
        self.assertEqual(cli.provider_argv(self.c,'new'), [cli.PROVIDERS['ollama'], 'run', 'qwen3:8b'])

    def test_picker_includes_home_history(self):
        self.assertEqual(cli.provider_argv(self.c, 'picker')[-2:], ['resume', '--all'])
        with patch.object(cli, 'saved_session', return_value=None):
            self.assertEqual(cli.provider_argv(self.c, 'resume')[-2:], ['resume', '--all'])

    def test_external_title_and_live_process_required(self):
        proc = self.path/'proc'/'123'
        (proc/'fd').mkdir(parents=True)
        (proc/'fd/0').symlink_to('/dev/pts/42')
        (proc/'cwd').symlink_to(self.path)
        (proc/'comm').write_text('codex')
        (proc/'cmdline').write_bytes(b'codex\0resume\0' + b'11111111-2222-3333-4444-555555555555\0')
        cache = self.path/'.cache/terminal-titles'
        cache.mkdir(parents=True)
        (cache/'_dev_pts_42').write_text(self.c['title'])
        with patch.object(cli, 'HOME', self.path), patch.object(cli, 'birth', return_value='123'):
            found = cli.external_session(self.c, self.path/'proc')
            self.assertEqual(found['pid'], 123)
            self.assertEqual(found['id'], '11111111-2222-3333-4444-555555555555')
            self.assertEqual(found['cwd'], str(self.path))
            with patch.object(cli, 'managed_ttys', return_value={'/dev/pts/42'}):
                self.assertIsNone(cli.external_session(self.c, self.path/'proc'))
            (cache/'_dev_pts_42').write_text('Unrelated')
            self.assertIsNone(cli.external_session(self.c, self.path/'proc'))

    def test_external_reuse_does_not_create_tmux_or_provider(self):
        with patch.object(cli, 'STATE', self.path), patch.object(cli, 'live', return_value=None), patch.object(cli, 'external_session', return_value={'pid':123}), patch.object(cli, 'reuse_external') as reuse, patch.object(cli, 'tmux', side_effect=AssertionError('duplicate')), patch.object(cli, 'validate_context', side_effect=AssertionError('new launch')):
            cli.up(self.c, ROOT/'config/workbench.example.yaml')
            reuse.assert_called_once()

    def test_binding_remembers_original_directory(self):
        identity = '11111111-2222-3333-4444-555555555555'
        with patch.object(cli, 'STATE', self.path), patch.object(cli, 'codex_sessions', return_value={identity:'/original/home'}):
            cli.bind_session(self.c, identity)
            self.assertEqual(cli.saved_session(self.c)['source_cwd'], '/original/home')
            self.assertEqual(cli.provider_argv(self.c, 'resume')[-2:], ['resume', identity])

    def test_new_conversation_is_remembered_and_ambiguity_rejected(self):
        with patch.object(cli, 'codex_sessions', return_value={'new':str(cli.cwd(self.c)), 'old':str(cli.cwd(self.c))}), patch.object(cli, 'bind_session') as bind:
            self.assertTrue(cli.remember_created(self.c, {'old':'elsewhere'}))
            bind.assert_called_once_with(self.c, 'new')
            with self.assertRaises(ValueError): cli.remember_created(self.c, {})

    def test_codex_startup_has_no_launcher_prompt(self):
        with patch.object(cli, 'run'), patch.object(cli, 'validate_context'), patch.object(cli.Path, 'cwd', return_value=self.path), patch.object(cli, 'start_codex', return_value=0) as start, patch('builtins.input', side_effect=AssertionError('unexpected prompt')), patch.dict(cli.os.environ, {}, clear=False):
            cli.menu(self.c)
            start.assert_called_once_with(self.c)

    def test_first_launch_creates_second_launch_resumes_same_id(self):
        history = {}
        calls = []
        identity = '11111111-2222-3333-4444-555555555555'
        def provider(args, **kwargs):
            calls.append(args)
            history[identity] = str(cli.cwd(self.c))
            return type('Result', (), {'returncode':0})()
        with patch.object(cli, 'external_session', return_value=None), patch.object(cli, 'STATE', self.path), patch.object(cli, 'validate_context'), patch.object(cli, 'checkout_keys', return_value=[]), patch.object(cli, 'reject_external_agents'), patch.object(cli, 'codex_sessions', side_effect=lambda:dict(history)), patch.object(cli, 'run', side_effect=provider):
            cli.start_codex(self.c)
            cli.start_codex(self.c)
        self.assertNotIn('resume', calls[0])
        self.assertEqual(calls[1][-2:], ['resume', identity])

    def test_up_restarts_only_an_exited_pane(self):
        with patch.object(cli, 'STATE', self.path), patch.object(cli, 'external_session', return_value=None), patch.object(cli, 'validate_context'), patch.object(cli, 'target', return_value='$1'), patch.object(cli, 'live', side_effect=[{'dead':True,'attached':0}, {'dead':False,'attached':0}]), patch.object(cli, 'tmux') as tmux, patch.object(cli, 'verify_pane_start') as verify, patch.object(cli, 'wait_provider'):
            cli.up(self.c, ROOT/'config/workbench.example.yaml', headless=True)
            self.assertEqual(tmux.call_args.args[0], 'respawn-pane')
            self.assertNotIn('-k', tmux.call_args.args)
            verify.assert_called_once_with(self.c, '$1')

    def test_new_session_has_explicit_headless_birth_geometry_and_verified_cwd(self):
        with patch.object(cli, 'STATE', self.path), patch.object(cli, 'external_session', return_value=None), patch.object(cli, 'validate_context'), patch.object(cli, 'required_target', return_value='$1'), patch.object(cli, 'live', side_effect=[None, {'dead':False,'attached':0}]), patch.object(cli, 'tmux') as tmux, patch.object(cli, 'verify_pane_start') as verify, patch.object(cli, 'wait_provider'):
            cli.up(self.c, ROOT/'config/workbench.example.yaml', headless=True)
        creation = tmux.call_args_list[0].args
        self.assertEqual(creation[:4], ('new-session', '-d', '-s', cli.session(self.c)))
        self.assertEqual(creation[creation.index('-x')+1], str(cli.DEFAULT_COLUMNS))
        self.assertEqual(creation[creation.index('-y')+1], str(cli.DEFAULT_ROWS))
        self.assertEqual(creation[creation.index('-c')+1], str(cli.cwd(self.c)))
        verify.assert_called_once_with(self.c, '$1', (cli.DEFAULT_COLUMNS, cli.DEFAULT_ROWS))

    def test_available_terminal_size_is_used_for_non_headless_birth(self):
        with patch.object(cli.shutil, 'get_terminal_size', return_value=os.terminal_size((132, 41))):
            self.assertEqual(cli.startup_size(False), (132, 41))
        with patch.object(cli.shutil, 'get_terminal_size', return_value=os.terminal_size((40, 10))):
            self.assertEqual(cli.startup_size(False), (80, 24))
        self.assertEqual(cli.startup_size(True), (cli.DEFAULT_COLUMNS, cli.DEFAULT_ROWS))

    def test_menu_refuses_fallback_directory_before_provider_start(self):
        with patch.object(cli, 'validate_context'), patch.object(cli.Path, 'cwd', return_value=self.path.parent), patch.object(cli, 'start_codex') as start:
            with self.assertRaisesRegex(ValueError, 'unexpected pane directory'):
                cli.menu(self.c)
        start.assert_not_called()

    def test_context_removed_after_validation_never_reports_provider_start(self):
        with patch.object(cli, 'STATE', self.path), patch.object(cli, 'external_session', return_value=None), patch.object(cli, 'validate_context', side_effect=[None, ValueError('Missing directory')]), patch.object(cli, 'required_target', return_value='$1'), patch.object(cli, 'live', return_value=None), patch.object(cli, 'tmux'), patch.object(cli, 'wait_provider') as wait:
            with self.assertRaisesRegex(ValueError, 'Missing directory'):
                cli.up(self.c, ROOT/'config/workbench.example.yaml', headless=True)
        wait.assert_not_called()

    def test_detached_managed_provider_reattaches_without_desktop_discovery(self):
        with patch.object(cli, 'STATE', self.path), patch.object(cli, 'validate_context'), patch.object(cli, 'live', return_value={'attached':0,'dead':False}), patch.object(cli, 'external_session', side_effect=AssertionError('managed provider misclassified')), patch.object(cli, 'open_tab') as opened, patch.object(cli, 'wait_provider'):
            cli.up(self.c, ROOT/'config/workbench.example.yaml')
            opened.assert_called_once()

    def test_lock_collision(self):
        with patch.object(cli,'STATE',self.path):
            with cli.lock('checkout',blocking=False):
                with self.assertRaises(BlockingIOError):
                    with cli.lock('checkout',blocking=False): pass

    def test_codex_early_retry_resumes_the_conversation_created_by_first_attempt(self):
        history, calls = {}, []
        identity = '11111111-2222-3333-4444-555555555555'
        def provider(args, **kwargs):
            calls.append(args)
            history[identity] = str(cli.cwd(self.c))
            return type('Result', (), {'returncode': 1 if len(calls) == 1 else 0})()
        with patch.object(cli, 'external_session', return_value=None), patch.object(cli, 'STATE', self.path), patch.object(cli, 'validate_context'), patch.object(cli, 'checkout_keys', return_value=[]), patch.object(cli, 'reject_external_agents'), patch.object(cli, 'codex_sessions', side_effect=lambda: dict(history)), patch.object(cli, 'run', side_effect=provider), patch.object(cli.time, 'sleep'):
            cli.retry_start(self.c, lambda: cli.start_codex(self.c))
        self.assertEqual(len(calls), 2)
        self.assertNotIn('resume', calls[0])
        self.assertEqual(calls[1][-2:], ['resume', identity])

    def test_one_context_failure_does_not_block_other_contexts(self):
        with patch.object(cli, 'up', side_effect=[ValueError('fixture startup failure'), None]) as up:
            with self.assertRaisesRegex(ValueError, 'Some contexts'):
                cli.main(['up', 'ai-workbench', 'claude-code', '--headless'])
        self.assertEqual(up.call_count, 2)

    def test_title_failure_does_not_prevent_codex_start(self):
        with patch.object(cli, 'run', side_effect=FileNotFoundError), patch.object(cli, 'validate_context'), patch.object(cli.Path, 'cwd', return_value=self.path), patch.object(cli, 'start_codex', return_value=0) as start:
            cli.menu(self.c)
            start.assert_called_once_with(self.c)

    def test_early_failure_retries_but_success_stops(self):
        with patch.object(cli.time, 'sleep') as sleep:
            operation = unittest.mock.Mock(side_effect=[1, 1, 0])
            cli.retry_start(self.c, operation)
            self.assertEqual(operation.call_count, 3)
            self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 2])

    def test_retry_is_bounded_and_never_restarts_normal_exit_signal_or_long_run(self):
        for code in [0, -2, 130, 143, 126, 127]:
            operation = unittest.mock.Mock(return_value=code)
            with patch.object(cli.time, 'sleep') as sleep:
                if code:
                    with self.assertRaises(ValueError): cli.retry_start(self.c, operation)
                else:
                    cli.retry_start(self.c, operation)
                self.assertEqual(operation.call_count, 1)
                sleep.assert_not_called()
        operation = unittest.mock.Mock(return_value=1)
        with patch.object(cli.time, 'monotonic', side_effect=[0, 11]), patch.object(cli.time, 'sleep') as sleep:
            with self.assertRaises(ValueError): cli.retry_start(self.c, operation)
            self.assertEqual(operation.call_count, 1)
            sleep.assert_not_called()
        operation.reset_mock()
        with patch.object(cli.time, 'sleep'):
            with self.assertRaises(ValueError): cli.retry_start(self.c, operation)
        self.assertEqual(operation.call_count, 3)

    def test_readiness_rejects_live_wrapper_and_preserves_it(self):
        state = {'dead': False, 'attached': 1, 'pane_pid': 42}
        with patch.object(cli, 'live', return_value=state), patch.object(cli, 'provider_pids', return_value=[]), patch.object(cli, 'tmux', side_effect=AssertionError('must not kill or respawn live wrapper')):
            with self.assertRaisesRegex(ValueError, 'pane preserved'):
                cli.wait_provider(self.c, timeout=0)

    def test_readiness_waits_for_stable_provider_and_detects_exited_pane(self):
        state = {'dead': False, 'attached': 1, 'pane_pid': 42}
        with patch.object(cli, 'live', return_value=state), patch.object(cli, 'provider_pids', side_effect=[[], [43], [43]]), patch.object(cli.time, 'sleep'):
            self.assertEqual(cli.wait_provider(self.c), state)
        with patch.object(cli, 'live', return_value={'dead': True}):
            with self.assertRaisesRegex(ValueError, 'exited during startup'): cli.wait_provider(self.c)

    def test_provider_presence_requires_descendant_non_zombie_expected_process(self):
        proc = self.path/'proc'
        for pid, parent, name, state in [(1, 0, 'python', 'S'), (2, 1, 'sh', 'S'), (3, 2, 'codex', 'S'), (4, 0, 'codex', 'S'), (5, 1, 'codex', 'Z')]:
            directory = proc/str(pid)
            directory.mkdir(parents=True)
            (directory/'comm').write_text(name)
            (directory/'stat').write_text(f'{pid} ({name}) {state} {parent}')
        self.assertEqual(cli.provider_pids(self.c, 1, proc), [3])

if __name__ == '__main__': unittest.main()
