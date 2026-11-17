"""Retriever: renderable flag, source, titles, ordering and timeouts."""

import asyncio
from typing import List

import pytest
from services.vector_search import retriever
from services.vector_search.backends.base import (
    BackendHit,
    BackendUnavailableError,
    SearchQuery,
)
from services.vector_search.index import STATUS_READY, VectorIndex
from services.vector_search.settings import VectorSearchSettings


class FixedBackend:
    """Returns canned hits (deliberately out of order) and records queries."""

    read_only = True

    def __init__(self, hits: List[BackendHit], delay: float = 0.0) -> None:
        self.hits = hits
        self.delay = delay
        self.queries: List[SearchQuery] = []

    async def search(self, query: SearchQuery) -> List[BackendHit]:
        self.queries.append(query)
        await asyncio.sleep(self.delay)
        return self.hits


def ready_index(
    backend: FixedBackend, url: str = "", timeout: str = "5"
) -> VectorIndex:
    settings = VectorSearchSettings.from_env(
        {
            "VECTOR_SEARCH_URL": url,
            "VECTOR_SEARCH_MODE": "external",
            "VECTOR_SEARCH_TIMEOUT_SECONDS": timeout,
        }
    )
    index = VectorIndex(settings, backend=backend, embedder=None, stac_api_url="")
    index.status = STATUS_READY
    return index


HITS = [
    BackendHit(id="unknown-external", score=0.4, title="Mystery"),
    BackendHit(id="mtbs", score=0.9),
]


def test_external_unknown_id_is_not_renderable_and_sorted() -> None:
    index = ready_index(FixedBackend(HITS), url="https://h/search")
    hits = asyncio.run(retriever.search("burn severity", 5, index=index))
    assert [h.id for h in hits] == ["mtbs", "unknown-external"]
    assert hits[0].renderable and not hits[1].renderable
    assert {h.source for h in hits} == {"external"}
    assert hits[0].title != "mtbs"  # filled from the local catalog


def test_managed_sources_and_private_renderable() -> None:
    backend = FixedBackend(
        [BackendHit(id="ortho", score=0.8), BackendHit(id="mtbs", score=0.7)]
    )
    settings = VectorSearchSettings.from_env({})
    index = VectorIndex(settings, backend=backend, embedder=None, stac_api_url="")
    index.status = STATUS_READY
    index.private_ids = {"ortho"}
    index.private_renderable = {"ortho"}
    index._embedder = _NoEmbedder()
    hits = asyncio.run(retriever.search("aerial photos", 5, index=index))
    assert [(h.id, h.source, h.renderable) for h in hits] == [
        ("ortho", "private_stac", True),
        ("mtbs", "planetary_computer", True),
    ]


class _NoEmbedder:
    model_name = "none"

    async def embed_query(self, text: str) -> List[float]:
        return [1.0]


def test_filters_are_passed_to_the_backend() -> None:
    backend = FixedBackend([])
    index = ready_index(backend, url="https://h/search")
    asyncio.run(
        retriever.search(
            "lidar",
            3,
            bbox=[-79.8, 45.0, -57.1, 62.6],
            datetime="2001-01-01/..",
            index=index,
        )
    )
    query = backend.queries[0]
    assert query.bbox == (-79.8, 45.0, -57.1, 62.6)
    assert query.datetime_text == "2001-01-01/.." and query.interval[1] is None


def test_slow_backend_is_unavailable_after_timeout() -> None:
    index = ready_index(
        FixedBackend(HITS, delay=1.0), url="https://h/search", timeout="0.05"
    )
    with pytest.raises(BackendUnavailableError, match="timed out"):
        asyncio.run(retriever.search("q", 3, index=index))


def test_not_ready_is_unavailable() -> None:
    index = ready_index(FixedBackend(HITS))
    index.status = "building"
    with pytest.raises(BackendUnavailableError, match="building"):
        asyncio.run(retriever.search("q", 3, index=index))


def test_malformed_datetime_is_a_value_error() -> None:
    with pytest.raises(ValueError):
        asyncio.run(
            retriever.search(
                "q", 3, datetime="last summer", index=ready_index(FixedBackend([]))
            )
        )
