"""Helpers for resolving SIMBAD identifiers."""

import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class SimbadLookupError(Exception):
    """Raised when a SIMBAD request could not be completed."""


def normalize_query(query_text: str) -> str:
    """Clean up whitespace and normalize common catalog prefixes."""
    cleaned = re.sub(r"\s+", " ", (query_text or "").strip())
    if not cleaned:
        return ""

    match = re.match(r"^(HD|HIP|GJ|TYC)\s*(\S.*)$", cleaned, flags=re.IGNORECASE)
    if match:
        prefix, rest = match.groups()
        cleaned = f"{prefix.upper()} {rest.strip()}"

    return cleaned.strip()


async def resolve_identity(query_text: str) -> dict | list[dict] | None:
    """Query SIMBAD for an object identity."""
    normalized = normalize_query(query_text)
    if not normalized:
        return None

    escaped = normalized.replace("'", "''")
    # Ask for one extra row so the UI can detect truncation.
    _DISPLAY_CAP = 10
    query = (
        f"SELECT TOP {_DISPLAY_CAP + 1} basic.main_id, basic.ra, basic.dec, basic.otype, basic.sp_type, ids.ids "
        "FROM basic JOIN ident ON basic.oid = ident.oidref JOIN ids ON basic.oid = ids.oidref "
        f"WHERE ident.id = '{escaped}'"
    )

    payload = {
        "request": "doQuery",
        "lang": "adql",
        "format": "json",
        "query": query,
    }

    try:
        # Use separate connect/read timeouts so unreachable hosts fail quickly.
        timeout = httpx.Timeout(connect=10.0, read=60.0, write=15.0, pool=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                "https://simbad.cds.unistra.fr/simbad/sim-tap/sync",
                data=payload,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            json_data = response.json()
    except httpx.TimeoutException as exc:
        # Treat timeouts as lookup failures rather than no-match results.
        logger.warning("SIMBAD lookup timed out for query_text=%r", normalized)
        raise SimbadLookupError(f"SIMBAD lookup timed out for {normalized!r}") from exc
    except httpx.HTTPError as exc:
        # Treat HTTP failures as lookup failures.
        logger.warning("SIMBAD lookup failed for query_text=%r", normalized, exc_info=True)
        raise SimbadLookupError(f"SIMBAD lookup failed for {normalized!r}") from exc
    except Exception as exc:
        # Treat parsing problems as lookup failures.
        logger.warning("SIMBAD lookup failed for query_text=%r", normalized, exc_info=True)
        raise SimbadLookupError(f"SIMBAD lookup failed for {normalized!r}") from exc

    rows: list[dict[str, Any]] = []
    if isinstance(json_data, dict):
        metadata = json_data.get("metadata")
        data = json_data.get("data")
        if isinstance(metadata, list) and isinstance(data, list):
            # SIMBAD's TAP service follows the standard IVOA TAP JSON envelope (the same
            # shape used by e.g. the Gaia archive): {"metadata": [{"name": ...}, ...],
            # "data": [[v1, v2, ...], ...]}. Each row is a POSITIONAL array, not a dict
            # keyed by column name -- the column order is only given once, in "metadata".
            # The previous code assumed every row was already a dict (`row.get("ids")`,
            # `isinstance(row, dict)`), which is the shape the *Exoplanet Archive* returns,
            # not SIMBAD. Against a real response every row failed the `isinstance(row, dict)`
            # check, so `rows` was always empty and resolve_identity() always returned None --
            # every query would have come back UNRESOLVED against the live API despite every
            # resolver/cache test passing, because those tests mock resolve_identity() itself
            # rather than exercising this parsing code.
            column_names = [
                col.get("name") if isinstance(col, dict) else None for col in metadata
            ]
            for raw_row in data:
                if isinstance(raw_row, dict):
                    rows.append(raw_row)
                elif isinstance(raw_row, (list, tuple)):
                    rows.append(
                        {
                            name: value
                            for name, value in zip(column_names, raw_row)
                            if name is not None
                        }
                    )
        elif isinstance(json_data.get("data"), list):
            rows = [row for row in json_data["data"] if isinstance(row, dict)]
        elif isinstance(json_data.get("results"), list):
            rows = [row for row in json_data["results"] if isinstance(row, dict)]
    elif isinstance(json_data, list):
        rows = [row for row in json_data if isinstance(row, dict)]

    if not rows:
        return None

    # Normalize each SIMBAD row into a consistent structure.
    candidates: list[dict[str, Any]] = []
    for row in rows:
        alias_field = row.get("ids") or row.get("ids.ids") or row.get("ids_ids") or row.get("idsids") or ""
        aliases = [item.strip() for item in str(alias_field).split("|") if item.strip()]
        candidate = {
            "main_id": row.get("main_id") or row.get("mainID") or row.get("MAIN_ID"),
            "ra": row.get("ra"),
            "dec": row.get("dec"),
            "otype": row.get("otype"),
            "sp_type": row.get("sp_type") or row.get("spType"),
            "aliases": aliases,
        }
        candidates.append(candidate)

    if len(candidates) == 1:
        return candidates[0]

    # Truncate large result sets and mark them as truncated.
    if len(candidates) > _DISPLAY_CAP:
        candidates = candidates[:_DISPLAY_CAP]
        for candidate in candidates:
            candidate["candidates_truncated"] = True

    return candidates
