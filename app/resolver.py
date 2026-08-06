"""Resolution logic."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from app.catalogs.exoplanet_archive import find_planets
from app.catalogs.simbad import SimbadLookupError, normalize_query, resolve_identity

logger = logging.getLogger(__name__)

_SIMBAD_TYPE_PREFIX_RE = re.compile(r"^(\*\*|V\*|\*)\s+")


def _without_simbad_type_prefix(name: str) -> str | None:
    """Strip SIMBAD object-type prefixes before alias comparison."""
    stripped = _SIMBAD_TYPE_PREFIX_RE.sub("", name)
    return stripped if stripped != name else None


@dataclass
class ResolutionResult:
    """Result of a query resolution attempt."""
    query_text: str
    state: str
    main_id: str | None = None
    ra: float | None = None
    dec: float | None = None
    otype: str | None = None
    spectral_type: str | None = None
    aliases: list[str] = field(default_factory=list)
    planets: list[dict[str, Any]] = field(default_factory=list)
    matched_alias: str | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    resolved_via: list[str] = field(default_factory=list)
    # Indicates whether a partial result came from a lookup failure.
    planets_lookup_failed: bool = False


async def resolve_query(query_text: str) -> ResolutionResult:
    """Resolve a catalog search through SIMBAD and the Exoplanet Archive."""
    resolve_started_at = time.perf_counter()
    normalized_query = normalize_query(query_text)
    simbad_started_at = time.perf_counter()
    try:
        simbad_result = await resolve_identity(normalized_query)
    except SimbadLookupError:
        logger.info("SIMBAD stage: failed after %.3fs", time.perf_counter() - simbad_started_at)
        # Treat lookup failures separately from genuine no-match results.
        return ResolutionResult(query_text=normalized_query, state="LOOKUP_FAILED")
    logger.info("SIMBAD stage: completed in %.3fs", time.perf_counter() - simbad_started_at)

    if simbad_result is None:
        return ResolutionResult(query_text=normalized_query, state="UNRESOLVED")

    if isinstance(simbad_result, list):
        return ResolutionResult(
            query_text=normalized_query,
            state="AMBIGUOUS",
            candidates=simbad_result,
        )

    aliases = list(simbad_result.get("aliases", []))
    # Try the canonical name first, then the aliases.
    main_id = simbad_result.get("main_id")
    match_candidates = list(aliases)
    if main_id and main_id not in match_candidates:
        match_candidates.insert(0, main_id)
    elif main_id in match_candidates:
        match_candidates.remove(main_id)
        match_candidates.insert(0, main_id)

    # Also try the stripped form for each identifier.
    alias_expansion_started_at = time.perf_counter()
    expanded_candidates: list[str] = []
    seen: set[str] = set()
    for candidate in match_candidates:
        for variant in (candidate, _without_simbad_type_prefix(candidate)):
            if variant and variant not in seen:
                expanded_candidates.append(variant)
                seen.add(variant)
    match_candidates = expanded_candidates
    logger.info(
        "Alias expansion stage: %d candidates in %.3fs",
        len(match_candidates),
        time.perf_counter() - alias_expansion_started_at,
    )

    # Query the exoplanet archive only when there is something to check.
    exoplanet_started_at = time.perf_counter()
    planets, matched_alias, planets_lookup_failed = (
        await find_planets(match_candidates) if match_candidates else ([], None, False)
    )
    logger.info(
        "Exoplanet Archive stage: completed in %.3fs (matched_alias=%r)",
        time.perf_counter() - exoplanet_started_at,
        matched_alias,
    )
    logger.info("resolve_query total: %.3fs for query_text=%r", time.perf_counter() - resolve_started_at, query_text)

    if planets:
        return ResolutionResult(
            query_text=normalized_query,
            state="RESOLVED",
            main_id=simbad_result.get("main_id"),
            ra=simbad_result.get("ra"),
            dec=simbad_result.get("dec"),
            otype=simbad_result.get("otype"),
            spectral_type=simbad_result.get("sp_type"),
            aliases=aliases,
            planets=planets,
            matched_alias=matched_alias,
            resolved_via=[
                step
                for step in (normalized_query, simbad_result.get("main_id"), matched_alias)
                if step
            ],
        )

    return ResolutionResult(
        query_text=normalized_query,
        state="PARTIAL",
        main_id=simbad_result.get("main_id"),
        ra=simbad_result.get("ra"),
        dec=simbad_result.get("dec"),
        otype=simbad_result.get("otype"),
        spectral_type=simbad_result.get("sp_type"),
        aliases=aliases,
        planets=planets,
        matched_alias=matched_alias,
        resolved_via=[step for step in (normalized_query, simbad_result.get("main_id")) if step],
        planets_lookup_failed=planets_lookup_failed,
    )
