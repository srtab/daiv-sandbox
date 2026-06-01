from __future__ import annotations

import logging
from datetime import UTC, datetime

logger = logging.getLogger("daiv_sandbox")


def _parse_docker_timestamp(value: str) -> datetime | None:
    """Parse a Docker RFC3339 timestamp (e.g. ``State.FinishedAt``) to an aware UTC datetime.

    Docker emits up to 9 fractional digits and a trailing ``Z`` (e.g.
    ``2026-06-01T12:34:56.123456789Z``). ``datetime.fromisoformat`` rejects >6 fractional digits,
    so truncate to microseconds. The zero value ``0001-01-01T00:00:00Z`` means "not set" (e.g. a
    still-running container) and maps to ``None``; unparseable input also maps to ``None``.
    """
    if not value or value.startswith("0001-01-01"):
        return None
    text = value[:-1] if value.endswith("Z") else value
    if "." in text:
        head, frac = text.split(".", 1)
        text = f"{head}.{frac[:6]}"
    try:
        return datetime.fromisoformat(text).replace(tzinfo=UTC)
    except ValueError:
        return None
