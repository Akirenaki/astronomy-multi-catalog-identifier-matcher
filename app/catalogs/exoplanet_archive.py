"""Helpers for querying the NASA Exoplanet Archive."""

import logging
import re
import time

import httpx

logger = logging.getLogger(__name__)

_BATCH_SIZE = 40

_EXOPLANET_ARCHIVE_URL = "https://exoplanetarchive.ipac.caltech.edu/TAP/sync"

# Same allow-list as app.catalogs.simbad -- see that module for rationale.
_ALLOWED_IDENTIFIER_CHARS = re.compile(r"^[A-Za-z0-9 +\-.'\"*]*$")


async def find_planets(alias_list: list[str]) -> tuple[list[dict], str | None, bool]:
    """Search the Exoplanet Archive for planets for a set of aliases."""
    if not alias_list:
        return [], None, False

    any_chunk_failed = False
    for chunk_start in range(0, len(alias_list), _BATCH_SIZE):
        chunk = alias_list[chunk_start : chunk_start + _BATCH_SIZE]
        rows_by_hostname = await _query_hostnames(chunk)
        if rows_by_hostname is None:
            # Keep going if a chunk fails so the search can still return a useful result.
            any_chunk_failed = True
            continue

        # Use the first matching alias in the chunk.
        for alias in chunk:
            rows = rows_by_hostname.get(alias)
            if rows:
                return _rows_to_planets(rows), alias, False

    return [], None, any_chunk_failed


async def _query_hostnames(aliases: list[str]) -> dict[str, list[dict]] | None:
    """Run one batched query for a chunk of aliases."""
    # Reject structurally hostile aliases before they're ever interpolated,
    # rather than failing the whole batch. Second layer of defense alongside
    # the quote-escaping below (TAP's doQuery sync endpoint doesn't offer
    # real bind parameters).
    safe_aliases = []
    for alias in aliases:
        if _ALLOWED_IDENTIFIER_CHARS.match(alias):
            safe_aliases.append(alias)
        else:
            logger.warning(
                "Exoplanet Archive lookup skipped alias %r: contains disallowed characters",
                alias,
            )
    if not safe_aliases:
        return {}

    # Escape embedded single quotes.
    escaped = [alias.replace("'", "''") for alias in safe_aliases]
    in_clause = ", ".join(f"'{value}'" for value in escaped)
    # No TOP limit here because a chunk can include multiple planets.
    query = (
        "SELECT pl_name, pl_letter, pl_orbper, pl_rade, disc_year, discoverymethod, hostname "
        f"FROM pscomppars WHERE hostname IN ({in_clause})"
    )

    payload = {
        "request": "doQuery",
        "lang": "adql",
        "format": "json",
        "query": query,
    }

    started_at = time.perf_counter()
    try:
        # Use a short connect timeout so unreachable hosts fail quickly.
        timeout = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                _EXOPLANET_ARCHIVE_URL,
                data=payload,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            # The Exoplanet Archive returns a bare JSON array of rows.
            body = response.json()
            rows = body if isinstance(body, list) else []
    except Exception:
        # Log chunk failures so they are distinguishable from genuine zero rows.
        logger.warning("Exoplanet Archive batched lookup failed for %d aliases", len(aliases), exc_info=True)
        return None
    finally:
        logger.info(
            "Exoplanet Archive stage: queried %d aliases in %.3fs",
            len(aliases),
            time.perf_counter() - started_at,
        )

    grouped: dict[str, list[dict]] = {}
    for row in rows:
        hostname = row.get("hostname") or row.get("HOSTNAME")
        if hostname:
            grouped.setdefault(hostname, []).append(row)
    return grouped


def _rows_to_planets(rows: list[dict]) -> list[dict]:
    """Convert raw Exoplanet Archive rows into the app's planet shape."""
    planets: list[dict] = []
    for row in rows:
        planets.append(
            {
                "pl_name": row.get("pl_name") or row.get("PL_NAME"),
                "pl_letter": row.get("pl_letter") or row.get("PL_LETTER"),
                "orbital_period_days": row.get("pl_orbper") or row.get("PL_ORBPER"),
                "planet_radius_earth": row.get("pl_rade") or row.get("PL_RADE"),
                "discovery_year": row.get("disc_year") or row.get("DISC_YEAR"),
                "discovery_method": row.get("discoverymethod") or row.get("DISCOVERYMETHOD"),
            }
        )
    return planets
