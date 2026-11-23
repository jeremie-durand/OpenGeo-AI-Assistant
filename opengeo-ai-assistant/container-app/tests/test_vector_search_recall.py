"""Retrieval recall@12 with the real bundled model on the evaluation set.

Runs only where the bundled model exists (the backend image); CI runs pytest
outside the image, so the test is skipped there.
"""

import asyncio
import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

import pytest
from services.vector_search.embedders import LocalEmbedder
from services.vector_search.evaluation import DEFAULT_QUESTIONS
from services.vector_search.index import VectorIndex
from services.vector_search.settings import (
    DEFAULT_MODEL,
    DEFAULT_MODEL_DIR,
    VectorSearchSettings,
)

EVAL_FILE = DEFAULT_QUESTIONS
MODEL_DIR = os.getenv("VECTOR_SEARCH_MODEL_DIR", DEFAULT_MODEL_DIR)
RECALL_K = 12
MIN_RECALL = 0.9


def _model_available() -> bool:
    return any(
        "paraphrase-multilingual-MiniLM-L12-v2" in name
        for name in (os.listdir(MODEL_DIR) if os.path.isdir(MODEL_DIR) else [])
    )


pytestmark = pytest.mark.skipif(
    not _model_available(), reason="bundled embedding model not present"
)


def test_retrieval_recall_at_12(tmp_path: Path) -> None:
    from services.vector_search.backends.parquet import ParquetBackend

    questions = json.loads(EVAL_FILE.read_text())["questions"]
    index = VectorIndex(
        VectorSearchSettings.from_env({}),
        backend=ParquetBackend(str(tmp_path / "i.parquet"), False, str(tmp_path)),
        embedder=LocalEmbedder(DEFAULT_MODEL, MODEL_DIR),
        stac_api_url="",
    )

    async def evaluate() -> Dict[str, List[bool]]:
        from services.vector_search import retriever

        await index.start()
        assert index.ready, index.reason
        found: Dict[str, List[bool]] = defaultdict(list)
        for item in questions:
            hits = await retriever.search(item["question"], RECALL_K, index=index)
            ids = {hit.id for hit in hits if hit.renderable}
            found[item["lang"]].append(bool(ids & set(item["acceptable"])))
        return found

    found = asyncio.run(evaluate())
    overall = [hit for hits in found.values() for hit in hits]
    report = {lang: sum(h) / len(h) for lang, h in found.items()}
    assert sum(overall) / len(overall) >= MIN_RECALL, report
    for lang, recall in report.items():
        assert recall >= MIN_RECALL, report
