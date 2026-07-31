"""Per-client rate limiting for Gemini summary generation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import RateLimitEvent

AI_SUMMARY_RATE_LIMIT = 20
AI_SUMMARY_RATE_LIMIT_WINDOW = timedelta(hours=1)

# KNOWN LIMITATION (TICKET-04 / Finding P2-2): check_limit() and record_usage()
# are separate transactions, with the actual Gemini call happening in between
# at each route's call site. Two concurrent requests from the same subject can
# both pass check_limit() before either calls record_usage(), letting a tight
# burst exceed the configured cap by however many requests race in the same
# window. check_and_record() below doesn't close this either -- it's the same
# two calls back-to-back, not a single atomic transaction.
#
# This is accepted as a soft cost-control limitation, not a security boundary:
# AI_SUMMARY_RATE_LIMIT bounds *steady-state* spend regardless of how much a
# single race window can overshoot it in the worst case, and the routes that
# use this limiter are gated behind login/session identity, not open to
# anonymous drive-by abuse at scale. If Gemini spend from this race becomes a
# real problem, the fix is to make record_usage() insert first and have the
# caller re-check the count including its own just-inserted row (narrowing the
# race window to the count query rather than the count query + a full Gemini
# round trip + a separate insert), or to serialize check+insert behind a
# `SELECT ... FOR UPDATE`-equivalent lock.


class RateLimitExceededError(Exception):
    """Raised when a subject has exceeded the allowed request count."""

    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Rate limit exceeded; retry after {retry_after_seconds}s")


async def check_limit(
    subject_type: str,
    subject_id: str,
    *,
    limit: int | None = None,
    window: timedelta | None = None,
) -> None:
    """Raise if a subject has already hit the request limit."""
    if limit is None:
        limit = AI_SUMMARY_RATE_LIMIT
    if window is None:
        window = AI_SUMMARY_RATE_LIMIT_WINDOW

    now = datetime.now(timezone.utc)
    window_start = now - window

    async with SessionLocal() as session:
        count_result = await session.execute(
            select(func.count()).where(
                RateLimitEvent.subject_type == subject_type,
                RateLimitEvent.subject_id == subject_id,
                RateLimitEvent.created_at > window_start,
            )
        )
        count = count_result.scalar_one()

        if count >= limit:
            oldest_result = await session.execute(
                select(RateLimitEvent.created_at)
                .where(
                    RateLimitEvent.subject_type == subject_type,
                    RateLimitEvent.subject_id == subject_id,
                    RateLimitEvent.created_at > window_start,
                )
                .order_by(RateLimitEvent.created_at.asc())
                .limit(1)
            )
            oldest = oldest_result.scalar_one_or_none()
            if oldest is not None:
                if oldest.tzinfo is None:
                    oldest = oldest.replace(tzinfo=timezone.utc)
                retry_after = (oldest + window) - now
                retry_after_seconds = max(1, int(retry_after.total_seconds()))
            else:
                retry_after_seconds = int(window.total_seconds())
            raise RateLimitExceededError(retry_after_seconds=retry_after_seconds)


async def record_usage(subject_type: str, subject_id: str) -> None:
    """Record one rate-limit event for a subject."""
    async with SessionLocal() as session:
        session.add(
            RateLimitEvent(subject_type=subject_type, subject_id=subject_id, created_at=datetime.now(timezone.utc))
        )
        await session.commit()


async def check_and_record(
    subject_type: str,
    subject_id: str,
    *,
    limit: int | None = None,
    window: timedelta | None = None,
) -> None:
    """Check and record in one call."""
    await check_limit(subject_type, subject_id, limit=limit, window=window)
    await record_usage(subject_type, subject_id)


# F6: rate_limit_events is a deliberately append-only event log (see the
# module comment above), so nothing ever deleted old rows -- over a
# long-lived deployment the table grows without bound. This retains rows
# for a full day, comfortably longer than any window currently in use
# (1 hour for AI summaries/resolves, 15 minutes for auth), and is invoked
# periodically by a lightweight in-process background task started from
# app.main's lifespan rather than requiring a separately-configured cron
# job (Render's Pre-Deploy/Cron features aren't available on the free tier).
RATE_LIMIT_EVENT_RETENTION = timedelta(hours=24)


async def purge_old_rate_limit_events(*, older_than: timedelta | None = None) -> int:
    """Delete rate_limit_events rows older than `older_than`. Returns row count deleted."""
    from sqlalchemy import delete

    cutoff = datetime.now(timezone.utc) - (older_than or RATE_LIMIT_EVENT_RETENTION)
    async with SessionLocal() as session:
        result = await session.execute(delete(RateLimitEvent).where(RateLimitEvent.created_at < cutoff))
        await session.commit()
        return result.rowcount or 0
