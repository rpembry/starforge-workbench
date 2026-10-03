import json
import os
from pathlib import Path
import socket
import threading

import pytest

from workbench.gpu_adapter import LocalAdapter, ReservationSocket
from workbench.gpu_policy import Observation as O
from workbench.gpu_registry import Registry, RegistryError


class FakeController:
    def __init__(self):
        self.process = O.PRESENT
        self.context = O.PRESENT
        self.idle = True
        self.ended = {}
        self.actions = []
        self.fail_after_apply = False

    def observe(self, _active_leases):
        return self.process, self.context, self.idle

    def workload_ended(self, owner):
        return self.ended.get(owner)

    def apply(self, action):
        self.actions.append(action)
        if action == 'STOP':
            self.process = self.context = O.ABSENT
        elif action == 'START':
            self.process = self.context = O.PRESENT
        else:
            raise AssertionError('Arbitrary action')
        if self.fail_after_apply:
            self.fail_after_apply = False
            raise RuntimeError('Synthetic crash after effect')


def setup(tmp_path, controller=None, time_value=None, boot='boot-one'):
    root = tmp_path / 'private'
    root.mkdir(mode=0o700, exist_ok=True)
    registry = Registry(root / 'leases.sqlite', boot)
    clock = time_value or [0.]
    return registry, LocalAdapter(registry, controller or FakeController(),
                                  clock_id=boot, clock=lambda: clock[0]), clock


def test_pending_stop_confirmed_exit_and_durable_restore(tmp_path):
    control = FakeController()
    registry, adapter, clock = setup(tmp_path, control)
    acquired = adapter.acquire('scope-a', 'request-a', 60)
    assert not acquired['granted']
    assert control.actions == []
    assert adapter.tick() == 'STOP'
    assert not adapter.status('scope-a', acquired['token'])['granted']
    assert adapter.tick() == 'HOLD'
    assert adapter.status('scope-a', acquired['token'])['granted']
    registry.close()
    registry = Registry(tmp_path / 'private' / 'leases.sqlite', 'boot-one')
    recovered = LocalAdapter(registry, control, clock_id='boot-one', clock=lambda: clock[0])
    assert recovered.acquire('scope-a', 'request-a', 60)['token'] == acquired['token']
    assert not recovered.status('scope-a', acquired['token'])['granted']
    recovered.tick()
    assert recovered.status('scope-a', acquired['token'])['granted']
    recovered.release('scope-a', acquired['token'])
    recovered.release('scope-a', acquired['token'])  # Lost release response.
    assert recovered.tick() == 'START'
    registry.close()


def test_restored_grant_is_fenced_before_fresh_miner_observation(tmp_path):
    control = FakeController()
    registry, adapter, clock = setup(tmp_path, control)
    acquired = adapter.acquire('scope-a', 'request-a', 60)
    adapter.tick()
    adapter.tick()
    assert adapter.status('scope-a', acquired['token'])['granted']
    registry.close()
    # The background process unexpectedly returned while the adapter was down.
    control.process = control.context = O.PRESENT
    registry = Registry(tmp_path / 'private' / 'leases.sqlite', 'boot-one')
    recovered = LocalAdapter(registry, control, clock_id='boot-one', clock=lambda: clock[0])
    assert not recovered.status('scope-a', acquired['token'])['granted']
    assert recovered.tick() == 'STOP'
    assert not recovered.status('scope-a', acquired['token'])['granted']
    assert recovered.tick() == 'HOLD'
    assert recovered.status('scope-a', acquired['token'])['granted']
    registry.close()


def test_exclusive_registry_owner_prevents_snapshot_overwrite(tmp_path):
    control = FakeController()
    registry, adapter, _ = setup(tmp_path, control)
    first = adapter.acquire('scope-a', 'request-a', 60)
    with pytest.raises(RegistryError, match='Another adapter'):
        Registry(tmp_path / 'private' / 'leases.sqlite', 'boot-one')
    registry.close()
    reopened = Registry(tmp_path / 'private' / 'leases.sqlite', 'boot-one')
    recovered = LocalAdapter(reopened, control, clock_id='boot-one', clock=lambda: 0)
    assert recovered.status('scope-a', first['token'])['token'] == first['token']
    reopened.close()


def test_crash_after_stop_effect_reobserves_before_grant(tmp_path):
    control = FakeController()
    registry, adapter, clock = setup(tmp_path, control)
    acquired = adapter.acquire('scope-a', 'request-a', 60)
    control.fail_after_apply = True
    with pytest.raises(RuntimeError, match='Synthetic crash'):
        adapter.tick()
    assert registry.pending_effect()[0] == 'STOP'
    registry.close()
    registry = Registry(tmp_path / 'private' / 'leases.sqlite', 'boot-one')
    recovered = LocalAdapter(registry, control, clock_id='boot-one', clock=lambda: clock[0])
    assert recovered.tick() == 'HOLD'
    assert recovered.status('scope-a', acquired['token'])['granted']
    assert control.actions == ['STOP']
    assert registry.pending_effect() is None
    registry.close()


def test_obsolete_start_intent_cannot_override_new_acquisition(tmp_path):
    control = FakeController()
    control.process = control.context = O.ABSENT
    registry, adapter, clock = setup(tmp_path, control)
    control.fail_after_apply = True
    with pytest.raises(RuntimeError):
        adapter.tick()
    assert registry.pending_effect()[0] == 'START'
    acquired = adapter.acquire('scope-a', 'request-a', 60)
    registry.close()
    registry = Registry(tmp_path / 'private' / 'leases.sqlite', 'boot-one')
    recovered = LocalAdapter(registry, control, clock_id='boot-one', clock=lambda: clock[0])
    assert recovered.tick() == 'STOP'
    assert control.actions == ['START', 'STOP']
    assert not recovered.status('scope-a', acquired['token'])['granted']
    registry.close()


def test_reboot_and_unknown_scope_hold_background_work(tmp_path):
    control = FakeController()
    registry, adapter, clock = setup(tmp_path, control)
    acquired = adapter.acquire('scope-a', 'request-a', 60)
    adapter.tick()
    adapter.tick()
    registry.close()
    clock[0] = 1
    registry = Registry(tmp_path / 'private' / 'leases.sqlite', 'next-boot')
    recovered = LocalAdapter(registry, control, clock_id='next-boot', clock=lambda: clock[0])
    assert recovered.status('scope-a', acquired['token'])['stale']
    assert recovered.tick() == 'HOLD'
    clock[0] = 61
    control.ended['scope-a'] = True
    assert recovered.tick() == 'START'
    registry.close()


def test_registry_rejects_insecure_path_and_corrupt_existing_state(tmp_path):
    root = tmp_path / 'private'
    root.mkdir(mode=0o700)
    target = root / 'real'
    target.write_text('not a database')
    os.chmod(target, 0o600)
    (root / 'link').symlink_to(target)
    with pytest.raises(RegistryError):
        Registry(root / 'link', 'boot')
    os.chmod(target, 0o644)
    with pytest.raises(RegistryError):
        Registry(target, 'boot')
    os.chmod(target, 0o600)
    with pytest.raises(RegistryError):
        Registry(target, 'boot')
    good = root / 'leases.sqlite'
    registry = Registry(good, 'boot')
    registry.db.execute("UPDATE policy SET snapshot=? WHERE id=1", (
        json.dumps({'schema': 1, 'clock_id': 'boot', 'leases': [
            {'owner': 'scope', 'token': 'token', 'expires_at': 10,
             'granted': 'yes', 'stale': False}], 'acquisitions': []}),))
    registry.close()
    with pytest.raises(RegistryError):
        Registry(good, 'boot')


def test_persist_failure_disables_adapter_instance(tmp_path, monkeypatch):
    registry, adapter, _ = setup(tmp_path)
    def fail(*_args, **_kwargs):
        raise RegistryError('Synthetic write failure')
    monkeypatch.setattr(registry, 'save', fail)
    with pytest.raises(RegistryError):
        adapter.acquire('scope', 'request', 60)
    with pytest.raises(RegistryError):
        adapter.status('scope', next(iter(adapter.policy.snapshot().leases)).token)
    registry.close()


def test_private_socket_binds_peer_and_rejects_caller_owner(tmp_path):
    registry, adapter, _ = setup(tmp_path)
    path = tmp_path / 'private' / 'adapter.sock'
    peers = []

    def resolve(pid, uid, gid):
        peers.append((pid, uid, gid))
        return 'supervised-scope'

    server = ReservationSocket(path, adapter, resolve)
    assert path.stat().st_mode & 0o777 == 0o600

    def request(data):
        worker = threading.Thread(target=server.handle_request)
        worker.start()
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(2)
            client.connect(str(path))
            client.sendall((json.dumps(data) + '\n').encode())
            answer = client.makefile('rb').readline()
        worker.join(timeout=2)
        assert not worker.is_alive()
        return json.loads(answer)

    try:
        result = request({'op': 'acquire', 'key': 'request-a', 'ttl': 60})
        assert result['ok'] and not result['result']['granted']
        token = result['result']['token']
        assert request({'op': 'status', 'token': token})['ok']
        assert not request({'op': 'release', 'token': token, 'owner': 'other'})['ok']
        assert peers and peers[0][1] == os.getuid()
        assert request({'op': 'release', 'token': token})['ok']
    finally:
        server.server_close()
        registry.close()
    assert not path.exists()


def test_stale_socket_recovery_rejects_live_and_non_socket_paths(tmp_path):
    registry, adapter, _ = setup(tmp_path)
    root = tmp_path / 'private'
    path = root / 'adapter.sock'
    resolver = lambda _pid, _uid, _gid: 'scope'
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(path))
    os.chmod(path, 0o600)
    stale.close()  # Leaves an owned, unbound socket filesystem entry.
    server = ReservationSocket(path, adapter, resolver)
    try:
        with pytest.raises(RegistryError, match='active'):
            ReservationSocket(path, adapter, resolver)
    finally:
        server.server_close()
    assert not path.exists()
    ordinary = root / 'ordinary'
    ordinary.write_text('fixture')
    with pytest.raises(RegistryError, match='not an owned'):
        ReservationSocket(ordinary, adapter, resolver)
    path.symlink_to(ordinary)
    with pytest.raises(RegistryError, match='not an owned'):
        ReservationSocket(path, adapter, resolver)
    assert path.is_symlink() and ordinary.read_text() == 'fixture'
    registry.close()
