"""Dataset search: find STAC collections by meaning, without an LLM call."""

import logging

from fastapi import APIRouter, HTTPException
from request_models import (
    DatasetSearchRequest,
    DatasetSearchResponse,
    DatasetSearchResult,
)
from services.vector_search import retriever
from services.vector_search.backends.base import (
    BackendConfigError,
    BackendUnavailableError,
)
from services.vector_search.index import get_index

logger = logging.getLogger(__name__)
router = APIRouter()

BACKEND_UNAVAILABLE = "The search backend is unavailable (unreachable or timed out)."
BACKEND_MISCONFIGURED = "The search backend is misconfigured; see the server logs."


@router.post("/api/datasets/search", response_model=DatasetSearchResponse)
async def dataset_search(body: DatasetSearchRequest) -> DatasetSearchResponse:
    """Return collections ranked by similarity; 503 when search is unavailable."""
    index = get_index()
    if not index.ready:
        raise HTTPException(status_code=503, detail=index.unavailable_message())
    try:
        hits = await retriever.search(
            body.query, body.k, body.bbox, body.datetime, index=index
        )
    except BackendConfigError as e:
        logger.warning(f"[VS] dataset search misconfigured: {e}")
        raise HTTPException(status_code=503, detail=BACKEND_MISCONFIGURED)
    except BackendUnavailableError as e:
        logger.warning(f"[VS] dataset search unavailable: {e}")
        raise HTTPException(status_code=503, detail=BACKEND_UNAVAILABLE)
    return DatasetSearchResponse(
        results=[
            DatasetSearchResult(
                id=hit.id,
                title=hit.title,
                score=hit.score,
                renderable=hit.renderable,
                source=hit.source,
            )
            for hit in hits
        ]
    )
