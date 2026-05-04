import json
import logging
import time

import log_store

logging.basicConfig(level=logging.INFO, format="%(message)s")
_log = logging.getLogger("ombud")


def log_tool_call(
    tool: str,
    candidate_id: str,
    inputs: dict,
    duration_ms: float,
    outcome: dict | None = None,
) -> None:
    entry = {
        "event": "tool_call",
        "tool": tool,
        "candidate_id": candidate_id,
        "inputs": inputs,
        "outcome": outcome or {},
        "duration_ms": round(duration_ms),
        "timestamp": time.time(),
    }
    _log.info(json.dumps(entry))
    log_store.append(entry)
