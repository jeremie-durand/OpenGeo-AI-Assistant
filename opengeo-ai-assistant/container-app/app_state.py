"""Runtime components shared by routers, populated by the app lifespan."""

from typing import Any, Optional

semantic_translator: Optional[Any] = None
global_translator: Optional[Any] = None
router_agent: Optional[Any] = None
SEMANTIC_KERNEL_AVAILABLE: bool = True
