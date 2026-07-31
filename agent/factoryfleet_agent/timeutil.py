"""One place where the agent decides what an instant looks like.

Timestamps cross three boundaries — the SQLite store, the MQTT payload, and the backend's
parser — and they have to mean the same thing at each. Everything is UTC with millisecond
precision and a trailing ``Z``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

#: Returns the current instant. Injected so tests can drive time instead of sleeping.
Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    """Formats an instant as ISO-8601 UTC, for example ``2026-07-30T04:15:00.000Z``.

    A naive datetime is assumed to be UTC rather than rejected: the alternative is an agent
    that crashes mid-cycle over a timezone, and the machine's clock is UTC in practice.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (
        moment.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def parse_iso(text: str) -> datetime:
    """Parses what :func:`iso` produces, back into an aware UTC datetime."""
    normalised = text.strip()
    if normalised.endswith("Z"):
        normalised = f"{normalised[:-1]}+00:00"
    parsed = datetime.fromisoformat(normalised)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
