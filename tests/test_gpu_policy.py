from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from workbench.gpu_policy import IdlePolicy, Lease, Observation as O, ReservationPolicy, Window


def step(policy, now=0, process=O.ABSENT, context=O.ABSENT,
         ended=lambda _: False, idle=True):
    return policy.reconcile(now=now, owned_process=process, owned_context=context,
                            workload_ended=ended, idle_allowed=idle)


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
    one = policy.acquire('client-a', 'request-one', 10, 60)
    two = policy.acquire('client-b', 'request-two', 10, 60)
    assert step(policy, process=O.PRESENT, context=O.PRESENT) == 'STOP'
    assert not policy.view('client-a', one.token).granted
    assert not policy.view('client-b', two.token).granted
    assert step(policy, process=O.ABSENT, context=O.UNKNOWN) == 'HOLD'
    assert not policy.view('client-a', one.token).granted
    assert step(policy) == 'HOLD'
    assert policy.view('client-a', one.token).granted
    assert policy.view('client-b', two.token).granted
    policy.release('client-a', one.token, 20)
    assert step(policy) == 'HOLD'
    policy.release('client-b', two.token, 20)
    assert step(policy) == 'START'


def test_expiry_client_crash_and_restart_are_conservative():
    policy = ReservationPolicy()
    lease = policy.acquire('supervised-client', 'request-one', 0, 10)
    assert step(policy) == 'HOLD' and policy.view('supervised-client', lease.token).granted
    restored = ReservationPolicy(policy.snapshot())
    assert step(restored, now=11, ended=lambda _: None) == 'HOLD'
    assert restored.view('supervised-client', lease.token).stale
    with pytest.raises(ValueError):
        restored.renew('supervised-client', lease.token, 11, 10)
    assert step(restored, now=12, ended=lambda _: True) == 'START'


def test_identity_scope_unknown_state_and_idle_gate():
    policy = ReservationPolicy()
    lease = policy.acquire('owner', 'request-one', 0, 60)
    with pytest.raises(PermissionError):
        policy.release('other', lease.token, 1)
    with pytest.raises(PermissionError):
        policy.renew('other', lease.token, 1, 20)
    assert step(policy, process=O.UNKNOWN) == 'HOLD'
    assert not policy.view('owner', lease.token).granted
    policy.release('owner', lease.token, 1)
    assert step(policy, process=O.UNKNOWN) == 'HOLD'
    assert step(policy, process=O.PRESENT, context=O.PRESENT, idle=False) == 'STOP'
    assert step(policy, idle=False) == 'HOLD'


def test_unexpected_background_restart_revokes_grant():
    policy = ReservationPolicy()
    lease = policy.acquire('owner', 'request-one', 0, 60)
    step(policy)
    assert policy.view('owner', lease.token).granted
    assert step(policy, process=O.PRESENT, context=O.PRESENT) == 'STOP'
    assert not policy.view('owner', lease.token).granted
    assert step(policy) == 'HOLD'
    assert policy.view('owner', lease.token).granted


def test_caller_cannot_mutate_pending_or_granted_state():
    policy = ReservationPolicy()
    returned = policy.acquire('owner', 'request-one', 0, 60)
    returned.granted = True
    returned.expires_at = 1
    assert not policy.view('owner', returned.token).granted
    assert policy.view('owner', returned.token).expires_at == 60
    step(policy, process=O.PRESENT)
    assert not policy.view('owner', returned.token).granted
    status = policy.view('owner', returned.token)
    status.granted = True
    assert not policy.view('owner', returned.token).granted


def test_parent_death_alone_cannot_clear_expired_hold():
    policy = ReservationPolicy()
    lease = policy.acquire('owner', 'request-one', 0, 10)
    step(policy)
    # Adapter cannot equate a dead parent PID with an ended workload scope.
    assert step(policy, now=11, ended=lambda _: None) == 'HOLD'
    assert policy.view('owner', lease.token).stale
    assert step(policy, now=12, ended=lambda _: True) == 'START'


def test_clock_rollback_never_shortens_renewed_hold():
    policy = ReservationPolicy()
    lease = policy.acquire('owner', 'request-one', 100, 60)
    renewed = policy.renew('owner', lease.token, 101, 10)
    assert renewed.expires_at == 160
    renewed.expires_at = 1
    assert policy.view('owner', lease.token).expires_at == 160


def test_lost_acquire_response_replays_across_restart_without_second_lease():
    policy = ReservationPolicy()
    original = policy.acquire('owner', 'stable-request', 0, 60)
    # Pretend the response was lost and the client retries after a restart.
    restored = ReservationPolicy(policy.snapshot())
    replay = restored.acquire('owner', 'stable-request', 1, 60)
    assert replay.token == original.token
    assert len(restored.snapshot().leases) == 1
    with pytest.raises(ValueError, match='different TTL'):
        restored.acquire('owner', 'stable-request', 2, 30)
    other = restored.acquire('another-owner', 'stable-request', 2, 60)
    assert other.token != original.token
    restored.release('owner', original.token, 3)
    with pytest.raises(ValueError, match='has ended'):
        restored.acquire('owner', 'stable-request', 4, 60)
    assert len(restored.snapshot().leases) == 1
    restarted = ReservationPolicy(restored.snapshot())
    with pytest.raises(ValueError, match='has ended'):
        restarted.acquire('owner', 'stable-request', 5, 60)
    # The ended-request tombstone is bounded; clients must not reuse keys.
    restarted.acquire('owner', 'stable-request', 3603, 60)
    assert len(restarted.snapshot().acquisitions) == 2


def test_wall_clock_jump_cannot_prune_tombstone_or_shorten_replay_window():
    policy = ReservationPolicy(clock_id='boot-one')
    lease = policy.acquire('owner', 'stable-request', 0, 60)
    policy.release('owner', lease.token, 3)
    wall_clock = 7200  # UTC time jumped forward by two hours.
    steady_clock = 4   # Boot-scoped monotonic time barely advanced.
    assert wall_clock > policy.snapshot().acquisitions[0].retain_until
    with pytest.raises(ValueError, match='has ended'):
        policy.acquire('owner', 'stable-request', steady_clock, 60)
    restored = ReservationPolicy(policy.snapshot(), clock_id='boot-one', now=steady_clock)
    with pytest.raises(ValueError, match='has ended'):
        restored.acquire('owner', 'stable-request', steady_clock, 60)


def test_reboot_rebases_retry_window_and_fences_active_lease():
    original = ReservationPolicy(clock_id='first-boot')
    live = original.acquire('live', 'live-request', 0, 60)
    ended = original.acquire('ended', 'ended-request', 0, 60)
    original.release('ended', ended.token, 2)
    restarted = ReservationPolicy(original.snapshot(), clock_id='next-boot', now=1)
    assert restarted.view('live', live.token).stale
    assert not restarted.view('live', live.token).granted
    assert step(restarted, now=2, ended=lambda _: None) == 'HOLD'
    with pytest.raises(ValueError, match='has ended'):
        restarted.acquire('ended', 'ended-request', 2, 60)
    assert restarted.snapshot().acquisitions[1].retain_until == 3601
    assert step(restarted, now=3601, ended=lambda _: True) == 'START'
    assert [r.owner for r in restarted.snapshot().acquisitions] == ['live']


def test_invalid_ttl_and_window_rejected():
    policy = ReservationPolicy()
    with pytest.raises(ValueError):
        policy.acquire('owner', 'request-one', 0, 0)
    with pytest.raises(ValueError):
        Window(300, 300)
