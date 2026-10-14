"""Notice agent turns that died, label them, and resume the ones a retry can fix.

A wrapped CLI whose turn fails goes idle at its prompt and says nothing to
anyone. Codex does it when a stream is cut mid-turn or a compaction request
drops (``task_complete`` with ``stream disconnected before completion``), and
on a usage limit. Each time, whoever handed the agent its work waits on a
reply that is not coming: on 2026-10-09 a forecasting agent sat idle for 47
hours after one cut stream.

This server service sweeps the sessions whose ``trax run`` is polling, reads
the tail of each one's newest part, and asks :func:`latest_turn` how its
latest turn ended. On a failure it:

- labels the session ``turn-failed`` (a ``change_log`` row, so ``trax recent``
  and every subscriber see it), and removes the label once a turn succeeds;
- for a TRANSIENT failure, queues a short "continue" message into the session
  after a backoff, at most ``max_resumes`` times before a turn succeeds again;
- for a usage limit that names when it lifts, queues the same message just
  after that time.

It only reads records and writes through the store, so it keeps no state a
restart would lose: the turns it has acted on are re-derived from the records
on the next sweep, with the resume count carried in memory alone (a restart
can at most resume a failure once more).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import asyncio
import logging
import re

from configgle import Fig

from trackinizer.addons.addon import AddonManifest, ServerContext
from trackinizer.lib.agent.turns import TurnState, latest_turn
from trackinizer.server.inbound import Inbound


if TYPE_CHECKING:
    from uuid import UUID

    from trackinizer.server.inbound import InboundQueue
    from trackinizer.server.store.core import Store


__all__ = ["FAILED_LABEL", "Supervisor", "retry_after"]


_LOGGER = logging.getLogger(__name__)

FAILED_LABEL = "turn-failed"
"""The label a session carries while its latest turn has failed."""

ACTOR = "trackinizer-supervisor"
"""The audit author of the supervisor's labels and the sender of its resumes."""

# How many trailing records to read per sweep. A turn's end is its newest
# record, so a short tail always holds it.
_TAIL = 200

# ``try again at Oct 14th, 2026 12:39 PM``: the moment a codex usage limit lifts.
_RETRY_AT = re.compile(
    r"(?i:try again at) (?P<month>[A-Z][a-z]{2}) (?P<day>\d{1,2})(?:st|nd|rd|th)?, "
    r"(?P<year>\d{4}) (?P<hour>\d{1,2}):(?P<minute>\d{2}) (?P<ampm>AM|PM)",
)


def retry_after(message: str | None, *, tz: timedelta = timedelta(0)) -> datetime | None:
    """Read when a usage limit lifts from its error message.

    Codex states the time in the user's local zone without naming it, so the
    caller says which offset that is.

    Args:
      message: The error message.
      tz: The offset of the zone the message's clock is in.

    Returns:
      when: The moment in UTC, or None if the message names none.

    """
    found = _RETRY_AT.search(message or "")
    if found is None:
        return None
    hour = int(found["hour"]) % 12 + (12 if found["ampm"] == "PM" else 0)
    local = datetime.strptime(
        f"{found['year']} {found['month']} {found['day']} {hour}:{found['minute']}",
        "%Y %b %d %H:%M",
    )
    return (local - tz).replace(tzinfo=UTC)


@dataclass(slots=True)
class _Watch:
    """What the supervisor knows about one session between sweeps."""

    failure: tuple[int, int] | None = None
    """The ``(part, idx)`` of the failed turn's end, while one is unresolved."""

    resumes: int = 0
    """Resumes queued since the session's last successful turn."""

    resume_at: datetime | None = None
    """When to queue the next resume, if one is due."""


class Supervisor:
    """Watch live agent sessions for failed turns, and resume transient ones."""

    class Config(Fig["Supervisor"]):
        interval_sec: float = 30.0
        """Seconds between sweeps."""

        backoff_sec: tuple[float, ...] = (60.0, 300.0, 900.0)
        """Delay before each successive resume of one failure streak; its
        length is how many resumes a streak gets."""

        max_age_sec: float = 6 * 3600.0
        """A failure older than this is labelled but never resumed: the work it
        was doing has likely been picked up some other way."""

        usage_limit_tz_hours: float = 2.0
        """The offset of the clock codex's usage-limit message is written in."""

        resume_text: str = (
            "Your last turn ended on an error that should now have cleared "
            "({error}). Continue the task you were working on from where you "
            "stopped."
        )
        """What a resume says; ``{error}`` is the error message, shortened."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def manifest(self) -> AddonManifest:
        """Contribute the sweep as one server service."""
        return AddonManifest(
            title="Supervisor",
            description=(
                "Labels agent sessions whose latest turn failed and resumes the "
                "ones a retry can fix."
            ),
            server_services=[SupervisorService(self.config)],
        )


@dataclass(slots=True)
class SupervisorService:
    """The sweep, as a long-running task inside the server."""

    config: Supervisor.Config
    name: str = "sweep"
    description: str = "Labels failed agent turns and resumes transient ones."
    watches: dict[UUID, _Watch] = field(default_factory=dict)

    async def run(self, context: ServerContext) -> None:
        """Sweep every ``interval_sec`` until cancelled."""
        while True:
            try:
                await self.sweep(context.store, context.inbound, now=datetime.now(UTC))
            except Exception:
                _LOGGER.exception("supervisor sweep failed; retrying next interval")
            await asyncio.sleep(self.config.interval_sec)

    async def sweep(self, store: Store, inbound: InboundQueue, *, now: datetime) -> None:
        """Check every polling session once.

        Args:
          store: The server's store.
          inbound: The live-session queue resumes go into.
          now: The current time.

        """
        live = set(inbound.active_poller_ids())
        for session_id in list(self.watches):
            if session_id not in live:
                del self.watches[session_id]
        for session_id in live:
            await self._check(store, inbound, session_id, now=now)

    async def _check(
        self,
        store: Store,
        inbound: InboundQueue,
        session_id: UUID,
        *,
        now: datetime,
    ) -> None:
        """Read one session's latest turn and act on it."""
        manifests = await store.read_session_manifests(session_id)
        if not manifests:
            return
        newest = max(manifests, key=lambda manifest: manifest.part)
        rows = await store.read_session_records(
            session_id,
            part=newest.part,
            after_idx=max(newest.records - _TAIL, 0) - 1,
            limit=_TAIL,
            plaintext_only=True,
        )
        if not rows:
            return
        state = latest_turn([row.record() for row in rows])
        first_sight = session_id not in self.watches
        watch = self.watches.setdefault(session_id, _Watch())
        if state.phase == "reply":
            # On first sight too: a label left by a sweep before a restart.
            if first_sight or watch.failure is not None or watch.resumes:
                await self._label(store, session_id, add=False)
            self.watches[session_id] = _Watch()
            return
        if state.phase != "error":
            return
        row = rows[state.index]
        key = (newest.part, row.idx)
        if watch.failure != key:
            # A new failure: label it, and plan its resume if one is due.
            watch.failure = key
            watch.resume_at = self._resume_at(
                state,
                failed_at=row.timestamp,
                now=now,
                resumes=watch.resumes,
            )
            if watch.resumes == 0:
                await self._label(store, session_id, add=True)
            _LOGGER.warning(
                "session %s turn failed (%s, transient=%s): %s",
                session_id,
                state.code,
                state.transient,
                state.text,
            )
        if watch.resume_at is None or now < watch.resume_at:
            return
        inbound.enqueue(
            session_id,
            Inbound(text=self._resume_text(state), source=ACTOR),
        )
        watch.resumes += 1
        watch.resume_at = None
        _LOGGER.warning("session %s resumed (attempt %d)", session_id, watch.resumes)

    def _resume_at(
        self,
        state: TurnState,
        *,
        failed_at: datetime | None,
        now: datetime,
        resumes: int,
    ) -> datetime | None:
        """When to resume a failure, or None if it should not be.

        ``resumes`` is how many this streak has had: each takes the next delay
        in ``backoff_sec``, and a streak that has used them all gets no more.
        """
        failed_at = failed_at or now
        backoff = self.config.backoff_sec
        if resumes >= len(backoff):
            return None
        if (now - failed_at).total_seconds() > self.config.max_age_sec:
            return None
        if state.transient:
            return failed_at + timedelta(seconds=backoff[resumes])
        lifts = retry_after(
            state.text,
            tz=timedelta(hours=self.config.usage_limit_tz_hours),
        )
        return None if lifts is None else lifts + timedelta(minutes=2)

    def _resume_text(self, state: TurnState) -> str:
        """The resume message for one failure."""
        error = (state.text or state.code or "unknown error").strip()
        if len(error) > 160:
            error = error[:157] + "..."
        return self.config.resume_text.format(error=error)

    async def _label(self, store: Store, session_id: UUID, *, add: bool) -> None:
        """Add or remove the failed-turn label; a failure is logged, not raised."""
        try:
            if add:
                await store.add_label(
                    session_id,
                    FAILED_LABEL,
                    actor=ACTOR,
                    reason="latest turn ended on an error",
                )
            else:
                await store.remove_label(
                    session_id,
                    FAILED_LABEL,
                    actor=ACTOR,
                    reason="a later turn succeeded",
                )
        except Exception:
            _LOGGER.exception("could not update %s on session %s", FAILED_LABEL, session_id)
