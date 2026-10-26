"""Queue fairness on the owner's FIFO: a full session cap queues instead of refusing (people before
automation), each queued row says why it waits, an edit keeps its place and cannot start while open,
and queued rows can be reordered."""
import asyncio
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from gateway.session_queue_holds import _slot_blocked, set_edit_hold
from hermes_state_runtime import list_session_admissions
from tests.waiting import async_wait_until
from tests.gateway.test_prompt_queued_controls import Turn, _call, _connection, interactive
from tests.gateway.test_session_authority_cancel_settlement import REF, _authority, _submit


def _queued(db):
    return [r['payload']['text'] for r in list_session_admissions(db, session_id='s') if r['status'] == 'queued']


def _reasons(authority):
    return {row['input_id']: row.get('hold_reason') for row in (
        asdict(authority._pending_receipt(r)) for r in authority._pending_rows(REF))}


@pytest.mark.asyncio
async def test_an_edit_keeps_its_place_and_a_reorder_moves_rows(tmp_path, monkeypatch):
    db, authority = _authority(tmp_path, monkeypatch)
    interactive(db)
    monkeypatch.setattr(authority, '_schedule', lambda ref: None)
    with db:
        await _submit(authority, 'one')
        two = await _submit(authority, 'two')
        three = await _submit(authority, 'three')
        conn = _connection(authority)

        edited = await _call(conn, 'prompt.replace_queued', admission_id=two.admission_id,
                             submission_id='two-edited', text='two, edited')
        assert 'error' not in edited, edited
        assert _queued(db) == ['one', 'two, edited', 'three'], 'the edit must keep its FIFO place'

        moved = await _call(conn, 'prompt.reorder_queued', admission_id=three.admission_id, position=0)
        assert 'error' not in moved, moved
        assert _queued(db) == ['three', 'one', 'two, edited']
        assert moved['result']['order'][0] == three.admission_id

        last = await _call(conn, 'prompt.reorder_queued', admission_id=three.admission_id, position=99)
        assert 'error' not in last, last
        assert _queued(db) == ['one', 'two, edited', 'three']


@pytest.mark.asyncio
async def test_a_held_head_never_starts_until_its_edit_ends(tmp_path, monkeypatch):
    db, authority = _authority(tmp_path, monkeypatch)
    interactive(db)
    turn = Turn(monkeypatch)
    real, drains = authority._schedule, {'on': False}
    monkeypatch.setattr(authority, '_schedule', lambda ref: real(ref) if drains['on'] else None)
    with db:
        conn = _connection(authority)
        head = await _submit(authority, 'head')
        held = await _call(conn, 'prompt.hold_queued', admission_id=head.admission_id, held=True)
        assert 'error' not in held, held
        assert _reasons(authority)['head'] == 'editing'
        drains['on'] = True
        authority._schedule(REF)
        # The drain reached the held head and parked on it instead of starting it.
        await async_wait_until(lambda: authority.hold_reasons.get('s') == 'editing', timeout=30)
        assert turn.ran == [], 'a held admission started mid-edit'

        released = await _call(conn, 'prompt.hold_queued', admission_id=head.admission_id, held=False)
        assert 'error' not in released, released
        await asyncio.wait_for(turn.claimed.wait(), 30)
        turn.release.set()
        await asyncio.wait_for(authority.sessions['s'].task, 30)
    assert turn.ran == ['head']


@pytest.mark.asyncio
async def test_a_full_session_cap_queues_the_turn_and_names_why(tmp_path, monkeypatch):
    db, authority = _authority(tmp_path, monkeypatch)
    interactive(db)
    running = {'count': 1}
    authority.runner.config.max_concurrent_sessions = 1
    authority.runner._is_session_running = lambda route: False
    authority.runner._running_agent_count = lambda: running['count']
    turn = Turn(monkeypatch)
    with db:
        await _submit(authority, 'waits for a slot')
        await async_wait_until(lambda: _reasons(authority).get('waits for a slot') == 'slot', timeout=30)
        assert turn.ran == [], 'the head started over a full session cap'
        running['count'] = 0  # another session finished
        await asyncio.wait_for(turn.claimed.wait(), 30)
        turn.release.set()
        await asyncio.wait_for(authority.sessions['s'].task, 30)
    assert turn.ran == ['waits for a slot']
    assert not _reasons(authority)


def test_a_freed_slot_goes_to_a_person_before_automation():
    runner = SimpleNamespace(config=SimpleNamespace(max_concurrent_sessions=1),
                             _is_session_running=lambda route: False, _running_agent_count=lambda: 0)
    authority = SimpleNamespace(runner=runner, profile_id='p')
    live = SimpleNamespace(route='r', source=SimpleNamespace(platform=None))
    cron = {'principal_id': 'cron-owner', 'payload': {'text': 'tick'}}
    person = {'principal_id': 'messaging:x', 'payload': {'text': 'hi'}}
    # The cron head waited first, but a person is also waiting: the person takes the one free slot.
    runner._running_agent_count = lambda: 1
    assert _slot_blocked(authority, SimpleNamespace(session_id='cron'), live, cron)
    assert _slot_blocked(authority, SimpleNamespace(session_id='chat'), live, person)
    runner._running_agent_count = lambda: 0
    assert _slot_blocked(authority, SimpleNamespace(session_id='cron'), live, cron)
    assert not _slot_blocked(authority, SimpleNamespace(session_id='chat'), live, person)
    assert not _slot_blocked(authority, SimpleNamespace(session_id='cron'), live, cron)


def test_an_edit_hold_lapses(monkeypatch):
    from gateway import session_queue_holds
    authority = SimpleNamespace(edit_holds={})
    set_edit_hold(authority, 'a', True)
    assert session_queue_holds.edit_held(authority, 'a')
    monkeypatch.setattr(session_queue_holds.time, 'monotonic', lambda: 10 ** 9)
    assert not session_queue_holds.edit_held(authority, 'a'), 'a crashed editor pinned the FIFO'
