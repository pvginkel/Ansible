"""Filters for comparing the role's desired OpenBao state with what OpenBao reads back."""

import re

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_DURATION = re.compile(r"(?:\d+[smhd])+")
_PART = re.compile(r"(\d+)([smhd])")


def openbao_duration_seconds(value):
    """Return a TTL as OpenBao reads it back: whole seconds.

    Accepts what the role's TTL vars hold — an int, a digit string, or a
    duration such as "30m" or "1h30m". Returns None for anything else, so
    a comparison against the read-back value counts as different and the
    write goes ahead rather than being skipped on a value we misread.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    if _DURATION.fullmatch(text):
        return sum(int(n) * _UNIT_SECONDS[u] for n, u in _PART.findall(text))
    return None


class FilterModule:
    def filters(self):
        return {"openbao_duration_seconds": openbao_duration_seconds}
