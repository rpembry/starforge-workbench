"""Opt-in wheel-routing check using only a provably owned fixture server."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest
from tmux_guard import FixtureTmuxTarget, cleanup_fixture_server, guarded_tmux_run


@pytest.mark.skipif(os.environ.get('WB_TEST_TMUX') != '1' or not shutil.which('tmux'),
                    reason='Set WB_TEST_TMUX=1 for isolated tmux integration')
@pytest.mark.parametrize('alternate,mouse', [(False,False),(True,False),(False,True),(True,True)])
def test_wheel_routing_tracks_terminal_modes(tmp_path, alternate, mouse):
    target=FixtureTmuxTarget(tmp_path)
    run=guarded_tmux_run(subprocess.run,target)
    config=Path(__file__).parents[1]/'config/tmux.conf'
    condition='#{&&:#{alternate_on},#{&&:#{mouse_any_flag},#{!:#{pane_in_mode}}}}'
    def tmux(*args):
        return run(['tmux','-f',str(config),'-S',str(target.socket),*args],
                   capture_output=True,text=True,check=True,timeout=3).stdout.strip()
    codes=('\x1b[?1049h' if alternate else '')+('\x1b[?1000h' if mouse else '')
    program=f'import sys,time;sys.stdout.write({codes!r});sys.stdout.flush();time.sleep(30)'
    try:
        tmux('new-session','-d','-s','renamed-provider',sys.executable,'-c',program)
        for _ in range(100):
            flags=tmux('display-message','-p','-t','renamed-provider','#{alternate_on}|#{mouse_any_flag}')
            if flags==f'{int(alternate)}|{int(mouse)}':break
            time.sleep(.01)
        assert flags==f'{int(alternate)}|{int(mouse)}'
        assert tmux('display-message','-p','-t','renamed-provider',condition)==str(int(alternate and mouse))
        bindings=tmux('list-keys','-T','root')
        assert bindings.count(condition)==2
        tmux('copy-mode','-t','renamed-provider')
        assert tmux('display-message','-p','-t','renamed-provider',condition)=='0'
        # Persisted file reload retains routing; context name has no special meaning.
        tmux('source-file',str(config))
        assert tmux('list-keys','-T','root').count(condition)==2
    finally:
        cleanup_fixture_server(target)
        target.close()
