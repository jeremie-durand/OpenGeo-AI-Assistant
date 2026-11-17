"""Retriever: text in, ranked collection hits with `renderable` and `source` out."""

import asyncio
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from pc_tasks_config_loader import get_collection_title
from services import dashboard_catalog

from .backends.base import BackendHit, BackendUnavailableError, SearchQuery
from .documents import (
    SOURCE_EXTERNAL,
    SOURCE_PLANETARY_COMPUTER,
    SOURCE_PRIVATE_STAC,
    Bbox,
    normalize_bbox,
    parse_datetime_filter,
)
from .index import VectorIndex, get_index
from .settings import MODE_EXTERNAL

KIND_COLLECTIONS = "collections"


@dataclass(frozen=True)
class SearchHit:
    """One ranked collection, ready for the query pipeline or the API."""

    id: str
    title: str
    description: str
    score: float
    source: str
    renderable: bool


def _pc_titles() -> Dict[str, str]:
    """Map every renderable Planetary Computer STAC id to its title."""
    return {
        stac_id: get_collection_title(config_id) or stac_id
        for config_id, stac_id in dashboard_catalog.renderable_collections().items()
    }


def _source(index: VectorIndex, collection_id: str, pc_ids: Sequence[str]) -> str:
    """Return where a hit comes from."""
    if index.settings.mode == MODE_EXTERNAL:
        return SOURCE_EXTERNAL
    if collection_id in index.private_ids and collection_id not in pc_ids:
        return SOURCE_PRIVATE_STAC
    return SOURCE_PLANETARY_COMPUTER


def to_search_hits(index: VectorIndex, hits: List[BackendHit]) -> List[SearchHit]:
    """Attach `renderable`, `source` and known titles, best score first."""
    titles = _pc_titles()
    renderable = index.renderable_ids
    results = [
        SearchHit(
            id=hit.id,
            title=hit.title or titles.get(hit.id, hit.id),
            description=hit.description,
            score=hit.score,
            source=_source(index, hit.id, list(titles)),
            renderable=hit.id in renderable,
        )
        for hit in hits
    ]
    return sorted(results, key=lambda hit: hit.score, reverse=True)


async def search(
    text: str,
    k: int = 10,
    bbox: Optional[Sequence[float]] = None,
    datetime: Optional[str] = None,
    kind: str = KIND_COLLECTIONS,
    index: Optional[VectorIndex] = None,
) -> List[SearchHit]:
    """Search collections by meaning.

    Raises BackendUnavailableError when vector search cannot answer (not ready,
    backend failure or timeout), and ValueError for a malformed datetime.
    """
    index = index or get_index()
    query_bbox: Optional[Bbox] = normalize_bbox(bbox) if bbox else None
    query = SearchQuery(
        text=text,
        k=k,
        kind=kind,
        bbox=query_bbox,
        interval=parse_datetime_filter(datetime) if datetime else None,
        datetime_text=datetime,
    )
    try:
        hits = await index.search(query)
    except asyncio.TimeoutError:
        raise BackendUnavailableError("Vector search timed out.") from None
    return to_search_hits(index, hits)
