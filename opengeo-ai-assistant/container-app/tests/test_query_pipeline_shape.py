"""The /api/query handler stays a thin orchestrator over small stages."""

import ast
import inspect
from pathlib import Path

PIPELINE = Path(__file__).parent.parent / "services" / "query_pipeline"
MAX_HANDLER_LINES = 70
MAX_STAGE_LINES = 150


def test_query_handler_is_thin() -> None:
    from routers.query import unified_query_processor

    source = inspect.getsource(unified_query_processor)
    assert len(source.splitlines()) <= MAX_HANDLER_LINES


def test_pipeline_functions_stay_small() -> None:
    oversized = [
        f"{path.name}:{node.name} ({node.end_lineno - node.lineno + 1} lines)"
        for path in sorted(PIPELINE.glob("*.py"))
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.end_lineno - node.lineno + 1 > MAX_STAGE_LINES
    ]
    assert not oversized, oversized
