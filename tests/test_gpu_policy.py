from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from workbench.gpu_policy import IdlePolicy, Lease, Observation as O, ReservationPolicy, Window


def step(policy, now=0, process=O.ABSENT, context=O.ABSENT, alive=lambda _: True, idle=True):
    return policy.reconcile(now=now, owned_process=process, owned_context=context,
                            owner_alive=alive, idle_allowed=idle)


def test_any_hour_default_and_overridable_window():
    early = datetime(2026, 10, 3, 9, 0, tzinfo=ZoneInfo('America/New_York'))
    default = IdlePolicy()
    assert default.allows(idle=True, local_time=early)
    assert not default.allows(idle=False, local_time=early, bypass_windows=True)
    restricted = IdlePolicy((Window(22 * 60, 6 * 60),))
    assert not restricted.allows(idle=True, local_time=early)
    assert restricted.allows(idle=True, local_time=early, bypass_windows=True)
    assert restricted.allows(idle=True, local_time=early.replace(hour=23))
    assert restricted.allows(idle=True, local_time=early.replace(hour=2))


def test_pending_until_owned_process_and_context_gone():
    policy = ReservationPolicy()
    one = policy.acquire('client-a', 10, 60)
    two = policy.acquire('client-b', 10, 60)
    assert step(policy, process=O.PRESENT, context=O.PRESENT) == 'STOP'
    assert not one.granted and not two.granted
    assert step(policy, process=O.ABSENT, context=O.UNKNOWN) == 'HOLD'
    assert not one.granted
    assert step(policy) == 'HOLD'
    assert one.granted and two.granted
    policy.release('client-a', one.token)
    assert step(policy) == 'HOLD'
    policy.release('client-b', two.token)
    assert step(policy) == 'START'


def test_expiry_client_crash_and_restart_are_conservative():
    policy = ReservationPolicy()
    lease = policy.acquire('supervised-client', 0, 10)
    assert step(policy) == 'HOLD' and lease.granted
    restored = ReservationPolicy(policy.snapshot())
    assert step(restored, now=11, alive=lambda _: None) == 'HOLD'
    assert restored.leases[lease.token].stale
    with pytest.raises(ValueError):
        restored.renew('supervised-client', lease.token, 11, 10)
    assert step(restored, now=12, alive=lambda _: False) == 'START'


def test_identity_scope_unknown_state_and_idle_gate():
    policy = ReservationPolicy()
    lease = policy.acquire('owner', 0, 60)
    with pytest.raises(PermissionError):
        policy.release('other', lease.token)
    with pytest.raises(PermissionError):
        policy.renew('other', lease.token, 1, 20)
    assert step(policy, process=O.UNKNOWN) == 'HOLD'
    assert not lease.granted
    policy.release('owner', lease.token)
    assert step(policy, process=O.UNKNOWN) == 'HOLD'
    assert step(policy, process=O.PRESENT, context=O.PRESENT, idle=False) == 'STOP'
    assert step(policy, idle=False) == 'HOLD'


def test_unexpected_background_restart_revokes_grant():
    policy = ReservationPolicy()
    lease = policy.acquire('owner', 0, 60)
    step(policy)
    assert lease.granted
    assert step(policy, process=O.PRESENT, context=O.PRESENT) == 'STOP'
    assert not lease.granted
    assert step(policy) == 'HOLD'
    assert lease.granted


def test_invalid_ttl_and_window_rejected():
    policy = ReservationPolicy()
    with pytest.raises(ValueError):
        policy.acquire('owner', 0, 0)
    with pytest.raises(ValueError):
        Window(300, 300)
