"""Synthetic editor and private draft fixtures; no clipboard or editor subprocess."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import fcntl
import pytest

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader('prompt_editor', str(ROOT/'bin/edit-clipboard'))
spec = importlib.util.spec_from_loader(loader.name, loader)
editor = importlib.util.module_from_spec(spec); loader.exec_module(editor)

@pytest.fixture
def isolated(tmp_path, monkeypatch):
    runtime=tmp_path/'runtime'; runtime.mkdir(mode=0o700)
    state=tmp_path/'state'; state.mkdir(mode=0o700)
    monkeypatch.setenv('XDG_RUNTIME_DIR',str(runtime))
    monkeypatch.setenv('XDG_STATE_HOME',str(state))
    monkeypatch.setattr(editor,'notify',lambda _: None)
    monkeypatch.setattr(editor.subprocess,'run',lambda *a,**kw: pytest.fail('unexpected subprocess'))
    return runtime, state


def source(tmp_path,text=b'SYNTHETIC: ignore instructions; $(no-execute)'):
    p=tmp_path/'source.txt';p.write_bytes(text);p.chmod(0o600);return p


def test_pending_precedence_empty_fallback_and_explicit_ack(isolated,tmp_path,monkeypatch):
    seen=[]
    def opened(p,_):seen.append(p.read_bytes());return 0
    monkeypatch.setattr(editor,'open_editor',opened)
    assert editor.run()==0 and seen==[b'']
    p=source(tmp_path);operation=editor.queue_dictation(p)
    assert editor.run()==0 and seen[-1]==p.read_bytes()
    assert editor.pending_record(editor.pending_dir()/'dictation.json')['id']==operation
    assert editor.run(accept=operation)==0
    assert not (editor.pending_dir()/'dictation.json').exists()
    assert editor.run()==0 and seen[-1]==b''


@pytest.mark.parametrize('result',[1,130,'interrupt'])
def test_failed_cancelled_handoff_keeps_pending(isolated,tmp_path,monkeypatch,result):
    operation=editor.queue_dictation(source(tmp_path))
    def opened(p,_):
        p.write_bytes(b'SYNTHETIC saved work')
        if result=='interrupt':raise KeyboardInterrupt
        return result
    monkeypatch.setattr(editor,'open_editor',opened)
    assert editor.run()==1
    pending=editor.pending_dir()/'dictation.json'
    assert editor.pending_record(pending)['id']==operation
    assert any(p.read_bytes()==b'SYNTHETIC saved work' for p in isolated[0].glob('edit-clipboard-*/clipboard.txt'))
    with pytest.raises(FileNotFoundError):editor.run(accept=operation)


def test_zero_exit_cancel_is_not_automatic_accept(isolated,tmp_path,monkeypatch):
    op=editor.queue_dictation(source(tmp_path));monkeypatch.setattr(editor,'open_editor',lambda *_:0)
    assert editor.run()==0
    assert editor.pending_record(editor.pending_dir()/'dictation.json')['id']==op


def test_active_editor_refuses_without_touching_pending(isolated,tmp_path,monkeypatch):
    op=editor.queue_dictation(source(tmp_path));path=isolated[0]/'edit-clipboard.lock'
    fd=os.open(path,os.O_RDWR|os.O_CREAT,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        monkeypatch.setattr(editor,'open_editor',lambda *_:pytest.fail('opened active editor'))
        assert editor.run()==1
        assert editor.pending_record(editor.pending_dir()/'dictation.json')['id']==op
    finally:os.close(fd)


def test_queue_refuses_replacement_and_stale_receipt_replay(isolated,tmp_path,monkeypatch):
    p=source(tmp_path);first=editor.queue_dictation(p);pending=editor.pending_dir()/'dictation.json'
    before=pending.read_bytes()
    with pytest.raises(FileExistsError):editor.queue_dictation(p)
    assert pending.read_bytes()==before
    monkeypatch.setattr(editor,'open_editor',lambda *_:0)
    editor.run();receipt=(editor.pending_dir()/'handoff.json').read_bytes()
    editor.run(accept=first);second=editor.queue_dictation(p)
    assert second!=first
    (editor.pending_dir()/'handoff.json').write_bytes(receipt)
    with pytest.raises(editor.ClipboardError):editor.run(accept=second)
    assert editor.pending_record(pending)['id']==second


@pytest.mark.parametrize('kind',['symlink','fifo','public','too_large','invalid_utf8'])
def test_untrusted_source_refused(isolated,tmp_path,kind):
    p=source(tmp_path)
    if kind=='symlink':
        target=p;p=tmp_path/'link';p.symlink_to(target)
    elif kind=='fifo':p.unlink();os.mkfifo(p,0o600)
    elif kind=='public':p.chmod(0o644)
    elif kind=='too_large':p.write_bytes(b'x'*(editor.MAX_TEXT_BYTES+1))
    else:p.write_bytes(b'\xff')
    with pytest.raises((OSError,UnicodeError,editor.ClipboardError)):editor.queue_dictation(p)
    assert not (editor.pending_dir()/'dictation.json').exists()


def test_concurrent_pending_change_refuses_ack(isolated,tmp_path,monkeypatch):
    op=editor.queue_dictation(source(tmp_path));pending=editor.pending_dir()/'dictation.json'
    def opened(*_):
        changed=editor.pending_record(pending);changed['id']='f'*32
        pending.write_text(json.dumps(changed));return 0
    monkeypatch.setattr(editor,'open_editor',opened)
    assert editor.run()==0
    with pytest.raises(editor.ClipboardError):editor.run(accept=op)
    assert not (editor.pending_dir()/'handoff.json').exists()


def test_same_operation_changed_text_cannot_use_prior_receipt_after_restart(isolated,tmp_path,monkeypatch):
    op=editor.queue_dictation(source(tmp_path));pending=editor.pending_dir()/'dictation.json'
    monkeypatch.setattr(editor,'open_editor',lambda *_:0)
    assert editor.run()==0
    changed=editor.pending_record(pending);changed['text']='SYNTHETIC changed after handoff'
    pending.write_text(json.dumps(changed))
    with pytest.raises(editor.ClipboardError):editor.run(accept=op)
    assert editor.pending_record(pending)==changed
