"""Command line: build, export or inspect the vector search index.

Usage (inside the backend container):
    python -m services.vector_search.cli build
    python -m services.vector_search.cli export --out /app/data/export.parquet
    python -m services.vector_search.cli status
    python -m services.vector_search.cli evaluate [--questions FILE]
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, List, Optional

from . import evaluation
from .backends.parquet import ParquetBackend
from .index import VectorIndex
from .settings import MODE_EXTERNAL, VectorSearchSettings

IndexFactory = Callable[[], VectorIndex]
TranslatorFactory = Callable[[], Any]


def _default_index() -> VectorIndex:
    """Return an index configured from the environment."""
    return VectorIndex(VectorSearchSettings.from_env())


def _default_translator() -> Any:
    """Return the query translator configured from the environment's LLM."""
    from generic_query_translator import GenericQueryTranslator

    return GenericQueryTranslator()


def _usable(index: VectorIndex) -> Optional[str]:
    """Return why *index* cannot be built, or None."""
    settings = index.settings
    if not settings.enabled:
        return "Vector search is disabled (VECTOR_SEARCH_ENABLED=false)."
    if settings.error:
        return settings.error
    if settings.mode == MODE_EXTERNAL:
        return "This backend is external (read-only); there is nothing to build."
    return None


async def build(index: VectorIndex) -> int:
    """Build or refresh the managed index and print what changed."""
    problem = _usable(index)
    if problem:
        print(problem, file=sys.stderr)
        return 2
    report = await index.build()
    print(
        f"added={report.added} updated={report.updated} "
        f"unchanged={report.unchanged} removed={report.removed} "
        f"embedded={report.embedded} total={index.count}"
    )
    return 0


async def export(index: VectorIndex, out: str) -> int:
    """Build the managed index, then write every document to a Parquet file."""
    status = await build(index)
    if status:
        return status
    rows = list((await index._backend.stored()).values())
    if os.path.exists(out):
        os.remove(out)
    target = ParquetBackend(out, False, index.settings.extension_dir)
    await target.upsert(rows)
    print(f"exported {len(rows)} documents to {out}")
    return 0


async def status(index: VectorIndex) -> int:
    """Check the backend once, without building, and print the health view."""
    settings = index.settings
    if not settings.enabled or settings.error:
        await index.start()
    elif await index._attempt(index.check):
        index.count = (await index._backend.describe()).count
    print(json.dumps(index.describe(), indent=2, default=str))
    return 0


async def evaluate(index: VectorIndex, translator: Any, questions: Path) -> int:
    """Compare the fixed list with retrieval candidates using a real LLM.

    Exits 1 when retrieval scores below the fixed list in any language.
    """
    if not questions.is_file():
        print(f"Evaluation set not found: {questions}", file=sys.stderr)
        return 2
    await index.start()
    if not index.ready:
        print(f"Vector search is not ready: {index.reason}", file=sys.stderr)
        return 2
    items = evaluation.load_questions(questions)
    report = await evaluation.evaluate(translator, index, items)
    print("\n".join(report.lines()))
    return 0 if report.passes() else 1


def main(
    argv: Optional[List[str]] = None,
    factory: IndexFactory = _default_index,
    translator_factory: TranslatorFactory = _default_translator,
) -> int:
    """Parse *argv* and run one command; returns the process exit code."""
    parser = argparse.ArgumentParser(prog="python -m services.vector_search.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("build", help="build or refresh the managed index")
    export_parser = commands.add_parser("export", help="write the index to Parquet")
    export_parser.add_argument("--out", required=True, help="Parquet file to write")
    commands.add_parser(
        "status", help="check the backend without building and print its status"
    )
    evaluate_parser = commands.add_parser(
        "evaluate", help="compare the fixed list with retrieval using the LLM"
    )
    evaluate_parser.add_argument(
        "--questions",
        type=Path,
        default=evaluation.DEFAULT_QUESTIONS,
        help="evaluation set (JSON)",
    )
    args = parser.parse_args(argv)
    index = factory()
    if args.command == "build":
        return asyncio.run(build(index))
    if args.command == "export":
        return asyncio.run(export(index, args.out))
    if args.command == "evaluate":
        return asyncio.run(evaluate(index, translator_factory(), args.questions))
    return asyncio.run(status(index))


if __name__ == "__main__":
    sys.exit(main())
