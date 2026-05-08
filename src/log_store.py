import json
import os
import time
import uuid
from collections import deque

_LOG_BUCKET = os.environ.get("LOG_BUCKET")
_LOG_PREFIX = "logs"
_MAX_FETCH = 200

_entries: deque[dict] = deque(maxlen=500)
_s3 = None

if _LOG_BUCKET:
    import boto3
    _s3 = boto3.client("s3")


def append(entry: dict) -> None:
    _entries.appendleft(entry)
    if _s3:
        _write_s3(entry)


def all_entries() -> list[dict]:
    if _s3:
        return _read_s3()
    return list(_entries)


def _write_s3(entry: dict) -> None:
    ts = entry.get("timestamp", time.time())
    key = f"{_LOG_PREFIX}/{int(ts * 1000):016d}-{uuid.uuid4().hex[:8]}.json"
    _s3.put_object(
        Bucket=_LOG_BUCKET,
        Key=key,
        Body=json.dumps(entry).encode(),
        ContentType="application/json",
    )


def _read_s3() -> list[dict]:
    paginator = _s3.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=_LOG_BUCKET, Prefix=f"{_LOG_PREFIX}/"):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])
    recent = keys[-_MAX_FETCH:]
    entries = []
    for key in reversed(recent):
        try:
            obj = _s3.get_object(Bucket=_LOG_BUCKET, Key=key)
            entries.append(json.loads(obj["Body"].read()))
        except Exception:
            pass
    return entries
