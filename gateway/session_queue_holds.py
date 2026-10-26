"""Why a queued admission waits, and the holds a session's drain honours before it claims its head.

A queued row is never refused for a reason that passes: the drain parks on its head and each pending
snapshot names the reason (``hold_reason``) so Desktop and ``/queue list`` can say why it waits.

- ``slot``: ``max_concurrent_sessions`` is full. The head waits instead of being answered with the
  refusal notice, and a freed slot goes to the best waiter: people before automation (cron, kanban,
  a2a, webhook, automation notices), then oldest first. Waiters are process-wide (one cap across
  every profile this runner serves) and refreshed on each poll, so a drain that stopped waiting
  (cancelled head, retirement) never keeps a slot from the others.
- ``editing``: a client is editing the row (``prompt.hold_queued``). The drain does not start it
  until the edit is saved or abandoned; the hold lapses after ``EDIT_HOLD_S`` so a crashed client
  cannot pin the FIFO.
- ``session_busy`` (a turn or worker runs on the session), ``restarting``, ``paused`` (a lost turn
  waits for the user): read from the drain's own pause reasons.

Other admission gates name their reason by writing ``authority.hold_reasons[session_id]`` while they
hold the head; the drain clears it once the FIFO moves.
"""
import logging
import time

logger = logging.getLogger(__name__)

EDIT_HOLD_S = 120.0
# A waiter not refreshed for this long stopped polling (its drain exited): it no longer ranks.
_WAITER_STALE_S = 2.0
_AUTOMATION_PRINCIPALS = ('cron-owner', 'automation:', 'a2a:', 'msgraph:')
_AUTOMATION_PAYLOADS = ('kanban_card_v1', 'local_automation_v1')
_PAUSE_REASONS = {'session_busy': 'session_busy', 'runtime_draining': 'restarting', 'unknown_execution': 'paused'}


def edit_held(authority, admission_id) -> bool:
    expiry = authority.edit_holds.get(admission_id)
    if expiry is None:
        return False
    if expiry > time.monotonic():
        return True
    authority.edit_holds.pop(admission_id, None)
    return False


def set_edit_hold(authority, admission_id, held: bool) -> None:
    now = time.monotonic()
    for stale in [aid for aid, expiry in authority.edit_holds.items() if expiry <= now]:
        del authority.edit_holds[stale]
    if held:
        authority.edit_holds[admission_id] = now + EDIT_HOLD_S
    else:
        authority.edit_holds.pop(admission_id, None)


def is_automation(row, platform=None) -> bool:
    """A turn no person is waiting at a chat for: it yields a freed slot to people."""
    payload = row.get('payload') or {}
    return (str(row.get('principal_id') or '').startswith(_AUTOMATION_PRINCIPALS)
            or any(key in payload for key in _AUTOMATION_PAYLOADS)
            or getattr(platform, 'value', platform) == 'webhook')


def _slot_blocked(authority, ref, live, row) -> bool:
    from hermes_cli.active_sessions import resolve_max_concurrent_sessions
    runner = authority.runner
    cap = resolve_max_concurrent_sessions(getattr(runner, 'config', None))
    if cap is None:
        return False
    waiters = runner.__dict__.setdefault('_queue_slot_waiters', {})
    key = (authority.profile_id, ref.session_id)
    if runner._is_session_running(live.route):
        waiters.pop(key, None)
        return False
    now = time.monotonic()
    rank = (1 if is_automation(row, getattr(live.source, 'platform', None)) else 0,
            waiters.get(key, (0, now))[1], key)
    waiters[key] = (*rank, now)
    for other in [k for k, entry in waiters.items() if now - entry[3] > _WAITER_STALE_S]:
        del waiters[other]
    free = cap - runner._running_agent_count()
    ahead = sum(1 for entry in waiters.values() if entry[:3] < rank)
    if ahead < free:
        waiters.pop(key, None)
        return False
    return True


def head_hold(authority, ref, live, row):
    """The reason the drain must not claim ``row`` (the queued head) yet, or None."""
    if edit_held(authority, row['admission_id']):
        return 'editing'
    if _slot_blocked(authority, ref, live, row):
        return 'slot'
    return None


def note_hold(authority, ref, reason) -> None:
    """The drain parks on its head for ``reason`` (None: it moved). Published once per change."""
    sid = ref.session_id
    previous = authority.hold_reasons.get(sid)
    reason = _PAUSE_REASONS.get(reason, reason)
    if reason == previous:
        return
    if reason is None:
        authority.hold_reasons.pop(sid, None)
    else:
        authority.hold_reasons[sid] = reason
        logger.info('Session %s queue held: %s', sid, reason)
    if sid in authority.sessions:
        authority._publish_pending(ref)


def hold_reason(authority, row):
    """Why the queued ``row`` is not running yet (``PendingAdmission.hold_reason``); None otherwise."""
    if row.get('status') != 'queued':
        return None
    if edit_held(authority, row['admission_id']):
        return 'editing'
    sid = row['target_session_id']
    if sid in authority.executing:
        return 'session_busy'
    return authority.hold_reasons.get(sid)
