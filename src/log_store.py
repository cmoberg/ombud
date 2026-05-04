from collections import deque

# In-memory store — ephemeral per Lambda container. Each cold start resets the log.
# For persistent logs, read from CloudWatch Logs.
_entries: deque[dict] = deque(maxlen=500)


def append(entry: dict) -> None:
    _entries.appendleft(entry)


def all_entries() -> list[dict]:
    return list(_entries)
