"""Compare instants, never the spelling of a caller's UTC offset."""
from datetime import datetime, timezone


def instant(value):
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    # Historical persisted rows used naive UTC. Never interpret them as the
    # operating system's local time, including during daylight-saving changes.
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def normalized(value):
    return instant(value).isoformat().replace('+00:00', 'Z')


def due(value, now):
    try:
        return instant(value) <= instant(now)
    except (ValueError, TypeError, OverflowError):
        return False
