"""Per-session pipeline trace cache returned in API responses for debugging."""

import json
import logging
from datetime import datetime, timezone
from typing import Dict, List

from cachetools import TTLCache

pipeline_traces: TTLCache = TTLCache(
    maxsize=500, ttl=1800
)  # 30-min TTL, max 500 sessions


def get_pipeline_trace(session_id: str) -> List[Dict]:
    """Get all trace entries for a session"""
    return pipeline_traces.get(session_id, [])


def clear_pipeline_trace(session_id: str):
    """Clear trace for a session (call at start of new query)"""
    pipeline_traces[session_id] = []


def log_pipeline_step(
    session_id: str, step_name: str, stage: str, data: dict, elapsed_ms: float = None
):
    """
    Log a pipeline step with structured format for easy filtering and debugging.

    Args:
        session_id: Unique identifier for this query session
        step_name: Name of the agent/step (e.g., "STAC_SEARCH", "TILE_URLS")
        stage: Either "INPUT" or "OUTPUT"
        data: Dictionary of relevant data to log
        elapsed_ms: Optional elapsed time in milliseconds
    """
    prefix = f"[PIPELINE:{session_id}]"
    timing = f" ({elapsed_ms:.0f}ms)" if elapsed_ms else ""

    # Truncate large data for readability
    data_str = json.dumps(data, default=str)
    if len(data_str) > 500:
        data_str = data_str[:500] + "..."

    logging.info(f"{prefix} {step_name} {stage}: {data_str}{timing}")

    # [MICRO] INSTANT TRACING: Also collect for API response
    if session_id and session_id != "N/A":
        if session_id not in pipeline_traces:
            pipeline_traces[session_id] = []

        trace_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "step": step_name,
            "stage": stage,
            "data": data,  # Keep full data for trace
        }
        if elapsed_ms:
            trace_entry["elapsed_ms"] = round(elapsed_ms, 1)

        pipeline_traces[session_id].append(trace_entry)
