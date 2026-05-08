import contextvars
import json
import logging
import time

import log_store

logging.basicConfig(level=logging.INFO, format="%(message)s")
_log = logging.getLogger("ombud")

source_ip: contextvars.ContextVar[str] = contextvars.ContextVar("source_ip", default="unknown")


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
        "source_ip": source_ip.get("unknown"),
    }
    _log.info(json.dumps(entry))
    log_store.append(entry)
