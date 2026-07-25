"""Per-client rate limiting for Gemini summary generation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import RateLimitEvent

AI_SUMMARY_RATE_LIMIT = 20
AI_SUMMARY_RATE_LIMIT_WINDOW = timedelta(hours=1)


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
