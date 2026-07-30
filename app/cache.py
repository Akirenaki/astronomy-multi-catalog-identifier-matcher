"""Cache helpers."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.database import SessionLocal
from app.models import (
    IdentifierRecord,
    ObjectRecord,
    PlanetRecord,
    QueryAlias,
    SavedSearch,
    UserSummarySnapshot,
)
from app.narrative import GeminiGenerationError, generate_summary
from app.resolver import ResolutionResult, resolve_query
from app.catalogs.simbad import normalize_query

logger = logging.getLogger(__name__)

AI_SUMMARY_COOLDOWN = timedelta(minutes=5)

# Ordering used to decide whether a fresh resolution is actually an improvement
# over what's already cached for an existing row, or a regression (e.g. a
# transient upstream failure on a routine TTL-refresh). Higher is better.
# See store_result(): a strictly-lower-quality new result must never overwrite
# a strictly-higher-quality existing one.
_STATE_QUALITY = {
    "RESOLVED": 4,
    "PARTIAL": 3,
    "AMBIGUOUS": 2,
    "UNRESOLVED": 1,
    "LOOKUP_FAILED": 0,
}


def _is_downgrade(
    new_state: str,
    new_planets_lookup_failed: bool,
    previous_state: str,
    previous_planets_lookup_failed: bool,
) -> bool:
    """Whether a fresh resolution is worse than what's already stored.

    Cross-state comparisons use _STATE_QUALITY. Same-state PARTIAL
    comparisons additionally treat a confirmed "no planets" result
    (planets_lookup_failed=False) as strictly better than an
    unconfirmed one (planets_lookup_failed=True), since both share
    the same _STATE_QUALITY score.
    """
    new_quality = _STATE_QUALITY[new_state]
    previous_quality = _STATE_QUALITY[previous_state]
    if new_quality != previous_quality:
        return new_quality < previous_quality
    if new_state == "PARTIAL":
        return new_planets_lookup_failed and not previous_planets_lookup_failed
    return False


class CooldownActiveError(Exception):
    """Raised when a summary regeneration is attempted too soon."""

    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Cooldown active; retry after {retry_after_seconds}s")


async def get_cached(query_text: str) -> ObjectRecord | None:
    """Return a fresh cached object for a query."""
    normalized_query = normalize_query(query_text)
    # Casefold only for the cache-key lookup, not for normalize_query()'s
    # actual output or what's sent to SIMBAD -- this just avoids fragmenting
    # the cache across differently-cased spellings of the same informal name
    # (e.g. "Betelgeuse" vs "betelgeuse"); see TICKET-07.
    key = (normalized_query or query_text).casefold()
    async with SessionLocal() as session:
        eager_opts = (selectinload(ObjectRecord.identifiers), selectinload(ObjectRecord.planets))

        result = await session.execute(
            select(ObjectRecord)
            .options(*eager_opts)
            .where(func.lower(ObjectRecord.query_text) == key, ObjectRecord.expires_at > datetime.now(timezone.utc))
        )
        hit = result.scalar_one_or_none()
        if hit is not None:
            return hit

        alias_result = await session.execute(
            select(ObjectRecord)
            .join(QueryAlias, QueryAlias.object_id == ObjectRecord.id)
            .options(*eager_opts)
            .where(func.lower(QueryAlias.query_text) == key, ObjectRecord.expires_at > datetime.now(timezone.utc))
        )
        return alias_result.scalar_one_or_none()


async def _upsert_query_aliases(session, query_texts: set[str], object_id: int) -> None:
    """Update alias rows for a resolved object."""
    for query_text in query_texts:
        if not query_text:
            continue
        existing = await session.execute(select(QueryAlias).where(QueryAlias.query_text == query_text))
        row = existing.scalar_one_or_none()
        if row is not None:
            row.object_id = object_id
        else:
            session.add(QueryAlias(query_text=query_text, object_id=object_id))


async def store_result(resolution_result: ResolutionResult, *, generate_ai_summary: bool = True) -> ObjectRecord:
    """Persist a resolution result to the database."""
    async with SessionLocal() as session:
        candidate_rows: list[ObjectRecord] = []

        by_query_text = await session.execute(
            select(ObjectRecord).where(ObjectRecord.query_text == resolution_result.query_text)
        )
        row = by_query_text.scalar_one_or_none()
        if row is not None:
            candidate_rows.append(row)

        if resolution_result.main_id:
            by_main_id = await session.execute(
                select(ObjectRecord).where(ObjectRecord.simbad_main_id == resolution_result.main_id)
            )
            row = by_main_id.scalar_one_or_none()
            if row is not None and row not in candidate_rows:
                candidate_rows.append(row)

        # Prefer updating an existing row in place to preserve its existing summary data.
        primary_row = candidate_rows[0] if candidate_rows else None
        stale_rows = candidate_rows[1:]
        old_query_texts = {r.query_text for r in candidate_rows}

        for stale_row in stale_rows:
            if primary_row is not None:
                # Re-point favorites/snapshots/aliases that reference the row
                # about to be deleted onto the surviving primary_row, so a
                # user's favorite or personal summary snapshot isn't silently
                # lost when store_result() merges two previously-separate
                # candidate rows into one (see TICKET-E).
                #
                # SavedSearch and UserSummarySnapshot both carry a
                # UniqueConstraint("user_id", "object_id"): if the same user
                # already has a row on both primary_row and stale_row, blindly
                # re-pointing would violate that constraint. Check for an
                # existing row on primary_row first and, if found, just drop
                # the stale duplicate instead (same check-first pattern as
                # add_favorite() elsewhere in this file).
                stale_saved_searches = (
                    await session.execute(select(SavedSearch).where(SavedSearch.object_id == stale_row.id))
                ).scalars().all()
                for saved_search in stale_saved_searches:
                    existing_on_primary = (
                        await session.execute(
                            select(SavedSearch).where(
                                SavedSearch.user_id == saved_search.user_id,
                                SavedSearch.object_id == primary_row.id,
                            )
                        )
                    ).scalar_one_or_none()
                    if existing_on_primary is not None:
                        await session.delete(saved_search)
                    else:
                        saved_search.object_id = primary_row.id

                stale_snapshots = (
                    await session.execute(
                        select(UserSummarySnapshot).where(UserSummarySnapshot.object_id == stale_row.id)
                    )
                ).scalars().all()
                for snapshot in stale_snapshots:
                    existing_on_primary = (
                        await session.execute(
                            select(UserSummarySnapshot).where(
                                UserSummarySnapshot.user_id == snapshot.user_id,
                                UserSummarySnapshot.object_id == primary_row.id,
                            )
                        )
                    ).scalar_one_or_none()
                    if existing_on_primary is not None:
                        await session.delete(snapshot)
                    else:
                        snapshot.object_id = primary_row.id

                # QueryAlias only has a UniqueConstraint on query_text, not on
                # (query_text, object_id), so there is no equivalent collision
                # risk here -- a plain re-point is safe.
                await session.execute(
                    update(QueryAlias).where(QueryAlias.object_id == stale_row.id).values(object_id=primary_row.id)
                )
                await session.flush()

            await session.delete(stale_row)
        if stale_rows:
            await session.flush()

        # Store the candidate list for later display.
        candidates_json = (
            json.dumps(resolution_result.candidates) if resolution_result.candidates else None
        )
        resolved_via_json = (
            json.dumps(resolution_result.resolved_via) if resolution_result.resolved_via else None
        )

        fresh_fields = dict(
            query_text=resolution_result.query_text,
            simbad_main_id=resolution_result.main_id,
            ra_deg=resolution_result.ra,
            dec_deg=resolution_result.dec,
            otype=resolution_result.otype,
            spectral_type=resolution_result.spectral_type,
            resolution_state=resolution_result.state,
            planets_lookup_failed=resolution_result.planets_lookup_failed,
            candidates_json=candidates_json,
            resolved_via_json=resolved_via_json,
            resolved_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(days=14),
        )

        is_downgrade = False
        if primary_row is not None:
            record = primary_row
            previous_state = record.resolution_state
            # Capture the previous planet set (by pl_name) *before* the
            # PlanetRecord rows are deleted below, so we can tell whether the
            # new resolution actually changed anything. Query directly rather
            # than via the `planets` relationship, since a lazy load isn't
            # safe to trigger implicitly under an AsyncSession.
            previous_planets_result = await session.execute(
                select(PlanetRecord.pl_name).where(PlanetRecord.object_id == record.id)
            )
            previous_planet_names = set(previous_planets_result.scalars().all())

            # Guard against a cache refresh that produced a *worse* result than
            # what's already stored -- most commonly a transient SIMBAD/Exoplanet
            # Archive failure hitting a row whose 14-day TTL just lapsed. Without
            # this check, a routine re-resolution failure would silently destroy
            # a previously-good, fully-resolved object (see TICKET-02 / P0-2).
            is_downgrade = _is_downgrade(
                resolution_result.state,
                resolution_result.planets_lookup_failed,
                previous_state,
                record.planets_lookup_failed,
            )

            if is_downgrade:
                # Leave simbad_main_id/ra_deg/dec_deg/otype/spectral_type/
                # resolution_state, the existing identifier/planet rows, and
                # ai_summary/ai_summary_generated_at untouched -- the new,
                # lower-quality result isn't allowed to overwrite them. Only
                # expires_at is refreshed below (using the short TTL for the
                # new attempt's failed/worse state), so a retry is still
                # scheduled soon.
                logger.warning(
                    "store_result(): new state %r is worse than existing state %r for "
                    "query_text=%r (id=%s); preserving existing data instead of overwriting.",
                    resolution_result.state,
                    previous_state,
                    resolution_result.query_text,
                    record.id,
                )
            else:
                for field_name, value in fresh_fields.items():
                    setattr(record, field_name, value)
                # Keep an existing summary when updating the row, UNLESS the new
                # resolution's state or planet list differs from what was
                # previously stored -- in that case the existing ai_summary would
                # describe stale data, so clear it and let the UI fall back to
                # "Generate AI summary".
                new_planet_names = {p.get("pl_name", "") for p in resolution_result.planets}
                if record.ai_summary is not None and (
                    previous_state != resolution_result.state or previous_planet_names != new_planet_names
                ):
                    record.ai_summary = None
                    record.ai_summary_generated_at = None
                await session.execute(delete(IdentifierRecord).where(IdentifierRecord.object_id == record.id))
                await session.execute(delete(PlanetRecord).where(PlanetRecord.object_id == record.id))
        else:
            record = ObjectRecord(**fresh_fields)
            session.add(record)
        await session.flush()

        # Short-lived cache entries are useful for unresolved or flaky lookups.
        if resolution_result.state in ("UNRESOLVED", "AMBIGUOUS", "LOOKUP_FAILED") or (
            resolution_result.state == "PARTIAL" and resolution_result.planets_lookup_failed
        ):
            record.expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

        # Store aliases as identifier rows. Skipped on a downgrade: the
        # existing IdentifierRecord rows were deliberately left in place above,
        # so inserting the (typically empty, but not always -- see the
        # PARTIAL+planets_lookup_failed case) new alias list here would just
        # create duplicates alongside the preserved data.
        if not is_downgrade:
            for alias in resolution_result.aliases:
                session.add(
                    IdentifierRecord(
                        object_id=record.id,
                        catalog="SIMBAD",
                        identifier=alias,
                        matched_exoplanet_archive=alias == resolution_result.matched_alias,
                    )
                )

            # Store planets when found.
            for planet in resolution_result.planets:
                session.add(
                    PlanetRecord(
                        object_id=record.id,
                        pl_name=planet.get("pl_name", ""),
                        pl_letter=planet.get("pl_letter"),
                        orbital_period_days=planet.get("orbital_period_days"),
                        planet_radius_earth=planet.get("planet_radius_earth"),
                        discovery_year=planet.get("discovery_year"),
                        discovery_method=planet.get("discovery_method"),
                    )
                )

        # Skip AI summaries for incomplete or failed lookups, and for a
        # downgrade (the new resolution_result's fields are the ones that
        # were just discarded above, so generating a summary from them would
        # describe data that isn't actually being stored).
        if (
            not is_downgrade
            and generate_ai_summary
            and record.ai_summary is None
            and resolution_result.state not in ("UNRESOLVED", "AMBIGUOUS", "LOOKUP_FAILED")
        ):
            summary_payload = {
                "main_id": resolution_result.main_id,
                "spectral_type": resolution_result.spectral_type,
                "planet_count": len(resolution_result.planets),
                "planets": resolution_result.planets,
            }
            try:
                record.ai_summary = await generate_summary(summary_payload)
                record.ai_summary_generated_at = datetime.now(timezone.utc)
            except GeminiGenerationError:
                # Defense in depth: any future caller of store_result(...,
                # generate_ai_summary=True) shouldn't have a Gemini hiccup
                # crash the whole request and roll back an otherwise-successful
                # catalog resolution. Leave ai_summary unset; the UI/API can
                # request one later via the rate-limited summary routes.
                logger.exception(
                    "Inline AI summary generation failed during store_result(); leaving ai_summary unset."
                )

        # Record the current and historical query strings for cache lookups.
        alias_query_texts = old_query_texts | {resolution_result.query_text}
        if resolution_result.main_id:
            alias_query_texts.add(normalize_query(resolution_result.main_id))
        await _upsert_query_aliases(session, alias_query_texts, record.id)

        commit_started_at = time.perf_counter()
        try:
            await session.commit()
        except IntegrityError:
            logger.info("Database commit stage: failed (IntegrityError) after %.3fs", time.perf_counter() - commit_started_at)
                # Another request won the race to insert this object; reuse that row.
            await session.rollback()
            fallback_query = select(ObjectRecord).options(
                selectinload(ObjectRecord.identifiers), selectinload(ObjectRecord.planets)
            )
            if resolution_result.main_id:
                fallback_query = fallback_query.where(ObjectRecord.simbad_main_id == resolution_result.main_id)
            else:
                fallback_query = fallback_query.where(ObjectRecord.query_text == resolution_result.query_text)
            result = await session.execute(fallback_query)
            winner = result.scalars().first()
            if winner is not None:
                return winner
            # Re-raise if the conflicting row disappeared before we could fetch it.
            raise
        logger.info("Database commit stage: completed in %.3fs", time.perf_counter() - commit_started_at)

        # Reload the row with relationships attached for later use.
        result = await session.execute(
            select(ObjectRecord)
            .options(selectinload(ObjectRecord.identifiers), selectinload(ObjectRecord.planets))
            .where(ObjectRecord.id == record.id)
        )
        return result.scalar_one()


async def get_or_resolve(query_text: str, *, generate_ai_summary: bool = True) -> ObjectRecord:
    """Return a cached result or resolve and store a new one."""
    cached = await get_cached(query_text)
    if cached is not None:
        return cached

    result = await resolve_query(query_text)
    return await store_result(result, generate_ai_summary=generate_ai_summary)


async def ensure_ai_summary(
    simbad_main_id: str,
    *,
    personal_api_key: str | None = None,
    personal_model: str | None = None,
) -> str:
    """Generate and persist an AI narrative if missing."""
    async with SessionLocal() as session:
        result = await session.execute(
            select(ObjectRecord)
            .options(selectinload(ObjectRecord.planets))
            .where(ObjectRecord.simbad_main_id == simbad_main_id)
        )
        record = result.scalar_one_or_none()
        if record is None:
            raise LookupError(f"No object found for simbad_main_id={simbad_main_id!r}")

        if record.ai_summary:
            # Reuse a cached summary when present.
            return record.ai_summary

        if record.resolution_state not in ("RESOLVED", "PARTIAL"):
            # Skip summaries for incomplete or failed lookups.
            return "No summary available."

        summary_payload = {
            "state": record.resolution_state,
            "main_id": record.simbad_main_id,
            "spectral_type": record.spectral_type,
            "planet_count": len(record.planets),
            "planets": [planet.to_dict() for planet in record.planets],
        }
        summary = await generate_summary(
            summary_payload, personal_api_key=personal_api_key, personal_model=personal_model
        )

        record.ai_summary = summary
        # Record when the summary was generated.
        record.ai_summary_generated_at = datetime.now(timezone.utc)
        await session.commit()
        return summary


async def regenerate_ai_summary(
    simbad_main_id: str,
    *,
    personal_api_key: str | None = None,
    personal_model: str | None = None,
) -> str:
    """Regenerate the AI narrative for a resolved object."""
    async with SessionLocal() as session:
        result = await session.execute(
            select(ObjectRecord)
            .options(selectinload(ObjectRecord.planets))
            .where(ObjectRecord.simbad_main_id == simbad_main_id)
        )
        record = result.scalar_one_or_none()
        if record is None:
            raise LookupError(f"No object found for simbad_main_id={simbad_main_id!r}")

        if record.resolution_state not in ("RESOLVED", "PARTIAL"):
            # Skip summaries for incomplete or failed lookups.
            return "No summary available."

        if record.ai_summary_generated_at is not None:
            generated_at = record.ai_summary_generated_at
            # Normalize the timestamp before comparing it.
            if generated_at.tzinfo is None:
                generated_at = generated_at.replace(tzinfo=timezone.utc)
            elapsed = datetime.now(timezone.utc) - generated_at
            if elapsed < AI_SUMMARY_COOLDOWN:
                retry_after = AI_SUMMARY_COOLDOWN - elapsed
                raise CooldownActiveError(retry_after_seconds=max(1, int(retry_after.total_seconds())))

        summary_payload = {
            "state": record.resolution_state,
            "main_id": record.simbad_main_id,
            "spectral_type": record.spectral_type,
            "planet_count": len(record.planets),
            "planets": [planet.to_dict() for planet in record.planets],
        }
        summary = await generate_summary(
            summary_payload, personal_api_key=personal_api_key, personal_model=personal_model
        )

        record.ai_summary = summary
        record.ai_summary_generated_at = datetime.now(timezone.utc)
        await session.commit()
        return summary


async def get_cached_ai_summary(simbad_main_id: str) -> str | None:
    """Return a cached AI summary if present."""
    async with SessionLocal() as session:
        result = await session.execute(
            select(ObjectRecord.ai_summary).where(ObjectRecord.simbad_main_id == simbad_main_id)
        )
        return result.scalar_one_or_none() or None


async def get_object_by_simbad_id(simbad_main_id: str) -> ObjectRecord | None:
    """Look up a stored object by its SIMBAD main identifier."""
    async with SessionLocal() as session:
        result = await session.execute(
            select(ObjectRecord)
            .options(selectinload(ObjectRecord.identifiers), selectinload(ObjectRecord.planets))
            .where(
                ObjectRecord.simbad_main_id == simbad_main_id,
                ObjectRecord.expires_at > datetime.now(timezone.utc),
            )
        )
        return result.scalar_one_or_none()


async def list_recent_objects(limit: int = 10) -> list[ObjectRecord]:
    """Return the newest object records, ordered from most recent to oldest."""
    async with SessionLocal() as session:
        result = await session.execute(
            select(ObjectRecord).order_by(ObjectRecord.resolved_at.desc()).limit(limit)
        )
        return list(result.scalars().all())


async def get_object_id_by_simbad_id(simbad_main_id: str) -> int | None:
    """Look up an object ID by SIMBAD id."""
    async with SessionLocal() as session:
        result = await session.execute(
            select(ObjectRecord.id).where(ObjectRecord.simbad_main_id == simbad_main_id)
        )
        return result.scalar_one_or_none()


async def add_favorite(user_id: int, object_id: int) -> SavedSearch:
    """Save an object as a favorite for a user."""
    async with SessionLocal() as session:
        existing = await session.execute(
            select(SavedSearch).where(SavedSearch.user_id == user_id, SavedSearch.object_id == object_id)
        )
        row = existing.scalar_one_or_none()
        if row is not None:
            return row

        row = SavedSearch(user_id=user_id, object_id=object_id)
        session.add(row)
        try:
            await session.commit()
        except IntegrityError:
            # Concurrent double-click race, same pattern as store_result()'s
            # candidate_rows handling: someone else's favorite for this exact pair
            # committed first. Treat it as success rather than a 500.
            await session.rollback()
            existing = await session.execute(
                select(SavedSearch).where(SavedSearch.user_id == user_id, SavedSearch.object_id == object_id)
            )
            return existing.scalar_one()
        await session.refresh(row)
        return row


async def remove_favorite(user_id: int, object_id: int) -> bool:
    """Unfavorite an object for a user. Returns True if a row was removed, False if
    it wasn't favorited in the first place (also not an error -- same idempotent
    reasoning as add_favorite).

    Unlike add_favorite(), this has no explicit IntegrityError handling -- there's
    no unique-constraint race to hit on a DELETE. A concurrent double-unfavorite
    (two requests racing between the SELECT and the DELETE) is still safe: whichever
    commits second simply issues a DELETE that matches zero rows, which SQLAlchemy
    treats as a normal no-op here (no version_id_col is configured on SavedSearch,
    so there's no optimistic-concurrency check to trip). See EVALUATION.md 1.7.
    """
    async with SessionLocal() as session:
        existing = await session.execute(
            select(SavedSearch).where(SavedSearch.user_id == user_id, SavedSearch.object_id == object_id)
        )
        row = existing.scalar_one_or_none()
        if row is None:
            return False
        await session.delete(row)
        await session.commit()
        return True


async def is_favorited(user_id: int, object_id: int) -> bool:
    """Whether this user has already favorited this object -- drives whether
    result.html renders a Favorite or Unfavorite button."""
    async with SessionLocal() as session:
        result = await session.execute(
            select(SavedSearch.id).where(SavedSearch.user_id == user_id, SavedSearch.object_id == object_id)
        )
        return result.scalar_one_or_none() is not None


async def list_favorites(user_id: int) -> list[dict]:
    """Return this user's favorited objects, newest favorite first, each paired with
    their personal summary snapshot if one exists (falling back to the shared
    canonical ai_summary otherwise -- see GET /account/saved's docstring in
    app/main.py for when that fallback applies)."""
    async with SessionLocal() as session:
        result = await session.execute(
            select(SavedSearch)
            .options(
                selectinload(SavedSearch.object).selectinload(ObjectRecord.identifiers),
                selectinload(SavedSearch.object).selectinload(ObjectRecord.planets),
            )
            .where(SavedSearch.user_id == user_id)
            .order_by(SavedSearch.created_at.desc())
        )
        saved_rows = list(result.scalars().all())

        object_ids = [row.object_id for row in saved_rows]
        snapshots_by_object_id: dict[int, str] = {}
        if object_ids:
            snapshot_result = await session.execute(
                select(UserSummarySnapshot).where(
                    UserSummarySnapshot.user_id == user_id,
                    UserSummarySnapshot.object_id.in_(object_ids),
                )
            )
            for snapshot in snapshot_result.scalars().all():
                snapshots_by_object_id[snapshot.object_id] = snapshot.summary_text

        return [
            {
                "object": row.object,
                "favorited_at": row.created_at,
                "personal_summary": snapshots_by_object_id.get(row.object_id),
                "used_fallback_summary": row.object_id not in snapshots_by_object_id,
            }
            for row in saved_rows
        ]


async def save_user_summary_snapshot(user_id: int, object_id: int, summary_text: str) -> None:
    """Persist a user's personal copy of an AI summary."""
    async with SessionLocal() as session:
        existing = await session.execute(
            select(UserSummarySnapshot).where(
                UserSummarySnapshot.user_id == user_id, UserSummarySnapshot.object_id == object_id
            )
        )
        row = existing.scalar_one_or_none()
        if row is not None:
            row.summary_text = summary_text
            row.created_at = datetime.now(timezone.utc)
        else:
            session.add(
                UserSummarySnapshot(user_id=user_id, object_id=object_id, summary_text=summary_text)
            )
        await session.commit()
