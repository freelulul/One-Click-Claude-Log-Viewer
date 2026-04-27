"""
Timezone-aware ISO timestamp handling.

Single source of truth for date/time conversion in the viewer. Every
date-by-grouping, clock-label, and "Day N of M" computation must go through
the helpers here. Never use file mtime, never use string slice timestamp[:10].
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone, date as date_cls
from typing import Iterable

try:
    from zoneinfo import ZoneInfo
    HAS_ZONEINFO = True
except ImportError:
    HAS_ZONEINFO = False


_FIXED_OFFSET_RE = re.compile(r"^([+-])(\d{2}):?(\d{2})$")


def resolve_tz(name: str | None) -> timezone | "ZoneInfo":
    """
    Resolve a tz spec to a tzinfo.

    Accepted forms:
      - None or "" or "local" -> system local tz (from datetime.now().astimezone())
      - "UTC" / "utc" / "Z"   -> timezone.utc
      - "+08:00" / "-05:00"   -> fixed offset
      - "America/Chicago" etc -> IANA via zoneinfo (requires py>=3.9)
    """
    if name is None or name == "" or name.lower() == "local":
        # Caller wants whatever the OS thinks "local" is.
        return datetime.now().astimezone().tzinfo  # type: ignore[return-value]

    if name.upper() in ("UTC", "Z"):
        return timezone.utc

    m = _FIXED_OFFSET_RE.match(name)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        hours = int(m.group(2))
        minutes = int(m.group(3))
        return timezone(sign * timedelta(hours=hours, minutes=minutes))

    if HAS_ZONEINFO:
        try:
            return ZoneInfo(name)
        except Exception as e:
            raise ValueError(
                f"Unknown timezone {name!r}: {e}. "
                "Pass an IANA name (America/Chicago), a fixed offset (+08:00), "
                "UTC, or omit --tz to use the system local tz."
            ) from e

    raise ValueError(
        f"Cannot resolve timezone {name!r}: zoneinfo not available "
        f"(Python {sys.version_info.major}.{sys.version_info.minor}; need 3.9+ "
        "for IANA names). Use a fixed offset like +08:00 or -05:00 instead."
    )


def tz_name(tz) -> str:
    """Best-effort human label for a tzinfo. Used in /api/config."""
    if tz is None:
        return "local"
    # ZoneInfo has .key; timezone objects expose name via tzname(None).
    key = getattr(tz, "key", None)
    if key:
        return key
    n = tz.tzname(None) if hasattr(tz, "tzname") else None
    return n or str(tz)


def parse_ts(s: str) -> datetime | None:
    """
    Parse an ISO 8601 timestamp from the jsonl. Returns timezone-aware datetime,
    or None if unparseable.

    Handles:
      - "2026-04-27T09:11:37.573Z"           (Z suffix; fromisoformat <3.11 chokes)
      - "2026-04-27T09:11:37.573000+00:00"
      - "2026-04-27T09:11:37+00:00"
      - "2026-04-27T09:11:37"                (naive: assume UTC)
    """
    if not s or not isinstance(s, str):
        return None
    raw = s.strip()
    # fromisoformat in <3.11 doesn't accept the 'Z' suffix.
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # Naive in source -> treat as UTC. Better than guessing local.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def local_date(ts: str, tz) -> str | None:
    """ISO timestamp -> 'YYYY-MM-DD' in the chosen tz, or None."""
    dt = parse_ts(ts)
    if dt is None:
        return None
    return dt.astimezone(tz).strftime("%Y-%m-%d")


def local_clock(ts: str, tz) -> str | None:
    """ISO timestamp -> 'HH:MM:SS' in the chosen tz."""
    dt = parse_ts(ts)
    if dt is None:
        return None
    return dt.astimezone(tz).strftime("%H:%M:%S")


def local_iso(ts: str, tz) -> str | None:
    """ISO timestamp -> ISO string in the chosen tz (kept aware)."""
    dt = parse_ts(ts)
    if dt is None:
        return None
    return dt.astimezone(tz).isoformat(timespec="seconds")


def day_n_of_m(active_dates: Iterable[str]) -> dict[str, tuple[int, int]]:
    """
    Given a set of 'YYYY-MM-DD' dates a session was active on, return
    {date: (n, total)} where n is the 1-based ordinal of that date among
    the *active* dates (not calendar days).

    Empty calendar gaps don't count: dates {2026-04-12, 2026-04-17, 2026-04-20}
    yield {2026-04-12: (1,3), 2026-04-17: (2,3), 2026-04-20: (3,3)}.
    """
    uniq = sorted(set(d for d in active_dates if d))
    total = len(uniq)
    return {d: (i + 1, total) for i, d in enumerate(uniq)}


def date_range_label(active_dates: Iterable[str]) -> str:
    """For session cards: '2026-04-12 ~ 2026-04-27' or single date."""
    uniq = sorted(set(d for d in active_dates if d))
    if not uniq:
        return ""
    if len(uniq) == 1:
        return uniq[0]
    return f"{uniq[0]} ~ {uniq[-1]}"


def is_valid_iso_date(s: str) -> bool:
    """Used to validate URL path components like /dates/<YYYY-MM-DD>/."""
    try:
        date_cls.fromisoformat(s)
        return True
    except (ValueError, TypeError):
        return False
