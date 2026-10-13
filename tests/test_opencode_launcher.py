import copy
import sqlite3
from unittest.mock import patch
import unittest
import test_launcher as base
cli = base.cli


class OpenCodeTests(unittest.TestCase):
    setUp = base.LauncherTests.setUp
    tearDown = base.LauncherTests.tearDown
    def test_opencode_exact_resume_and_archive_rejection(self):
        self.c.update(provider='opencode', id='opencode')
        db=self.path/'.local/share/opencode/opencode.db';db.parent.mkdir(parents=True)
        with sqlite3.connect(db) as conn:
            conn.execute('create table session(id text,directory text,parent_id text,time_archived integer)')
            conn.execute('insert into session values(?,?,null,null)',('ses_fixture',str(self.path)))
            conn.execute('insert into session values(?,?,?,null)',('ses_child',str(self.path),'ses_fixture'))
        with patch.object(cli,'HOME',self.path),patch.object(cli,'STATE',self.path/'state'):
            self.assertEqual(cli.provider_sessions(self.c),{'ses_fixture':str(self.path)})
            self.assertNotIn('--model',cli.provider_argv(self.c,'new'))
            self.assertNotIn('--auto',cli.provider_argv(self.c,'new'))
            with self.assertRaises(ValueError):cli.provider_argv(self.c,'resume')
            cli.bind_session(self.c,'ses_fixture')
            args=cli.provider_argv(self.c,'resume')
            self.assertEqual(args[-2:],['--session','ses_fixture'])
            self.assertNotIn('--continue',args)
            with sqlite3.connect(db) as conn:conn.execute('update session set time_archived=1 where id=?',('ses_fixture',))
            with self.assertRaises(ValueError):cli.saved_session(self.c)
