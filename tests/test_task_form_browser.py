"""Set WB_PLAYWRIGHT_MODULE to the installed playwright-core module to run."""
import shutil
import socket
import threading
import time
import pytest
import uvicorn
from workbench.auth import Auth
from workbench.main import create_app
from workbench.repository import SQLiteRepository


def test_two_successful_tasks_reset_and_failure_preserves_draft(tmp_path):
    import os
    import subprocess
    from pathlib import Path
    module=os.environ.get('WB_PLAYWRIGHT_MODULE')
    chrome=shutil.which('google-chrome')
    if not module or not chrome:pytest.skip('Set WB_PLAYWRIGHT_MODULE to installed playwright-core; requires Chrome')
    repo=SQLiteRepository(tmp_path/'state'/'workbench.sqlite')
    app=create_app(repo,Auth({'operator':'o'*40,'collector':'c'*40}))
    listener=socket.socket();listener.bind(('127.0.0.1',0))
    port=listener.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,log_level='error'))
    thread=threading.Thread(target=server.run,kwargs={'sockets':[listener]},daemon=True);thread.start()
    try:
        for _ in range(100):
            if server.started:break
            time.sleep(.05)
        assert server.started
        subprocess.run(['node',str(Path(__file__).with_name('task_form_browser.cjs'))],check=True,
                       env={**os.environ,'WB_CHROME':chrome,'WB_TEST_URL':f'http://127.0.0.1:{port}/'},timeout=60)
        assert {a['title'] for a in repo.list('actions')}=={'First browser fixture','Second browser fixture'}
    finally:
        server.should_exit=True;thread.join(timeout=5);listener.close()
