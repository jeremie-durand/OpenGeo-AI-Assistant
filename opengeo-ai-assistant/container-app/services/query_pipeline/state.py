"""Shared request inputs and intermediate results of the /api/query stages."""

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class QueryState:
    """Everything the /api/query stages read and write, in pipeline order."""

    natural_query: str = ""
    session_id: Optional[str] = None
    pin: Optional[Dict[str, Any]] = None
    req_body: Dict[str, Any] = field(default_factory=dict)
    pipeline_session_id: str = ""
    router_action: Optional[Dict[str, Any]] = None
    classification: Optional[Dict[str, Any]] = None
    intent_type: Optional[str] = None
    confidence: float = 0
    translator: Optional[Any] = None
    vision_task: Optional["asyncio.Task[Any]"] = None
    stac_params: Optional[Dict[str, Any]] = None
    stac_query: Optional[Dict[str, Any]] = None
    stac_endpoint: Optional[str] = None
    stac_response: Dict[str, Any] = field(default_factory=dict)
    features: List[Dict[str, Any]] = field(default_factory=list)
    search_diagnostics: Dict[str, Any] = field(default_factory=dict)
    response_message: str = ""
    all_tile_urls: List[Dict[str, Any]] = field(default_factory=list)
    mosaic_result: Optional[Dict[str, Any]] = None
