"""Embedding models: the bundled local model or an OpenAI-compatible endpoint."""

import asyncio
from typing import Any, Callable, List, Optional, Protocol

from cachetools import LRUCache

from .settings import (
    DEFAULT_MODEL,
    PROVIDER_LOCAL,
    PROVIDER_OPENAI,
    VectorSearchSettings,
)

QUERY_CACHE_SIZE = 256
LOCAL_BATCH_SIZE = 32
REMOTE_BATCH_SIZE = 64
DIMENSION_PROBE = "dimension probe"


class Embedder(Protocol):
    """Turns text into vectors; every method is safe to await on the event loop."""

    model_name: str

    async def dimension(self) -> int:
        """Return the vector dimension the model produces."""
        ...

    async def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """Return one vector per document text."""
        ...

    async def embed_query(self, text: str) -> List[float]:
        """Return the vector of a search query."""
        ...


class _QueryCache:
    """Small LRU of query vectors, keyed by query text."""

    def __init__(self) -> None:
        self._cache: LRUCache = LRUCache(maxsize=QUERY_CACHE_SIZE)

    async def get(self, text: str, compute: Callable[[str], Any]) -> List[float]:
        """Return the cached vector for *text*, computing it on a miss."""
        if text not in self._cache:
            self._cache[text] = await compute(text)
        return list(self._cache[text])


class LocalEmbedder:
    """fastembed ONNX model, loaded lazily and run off the event loop."""

    def __init__(self, model_name: str, cache_dir: str) -> None:
        self.model_name = model_name
        self._cache_dir = cache_dir
        self._model: Any = None
        self._dimension: Optional[int] = None
        self._lock = asyncio.Lock()
        self._queries = _QueryCache()

    def _load(self) -> Any:
        """Load the model; the bundled one must already be on disk."""
        from fastembed import TextEmbedding

        return TextEmbedding(
            self.model_name,
            cache_dir=self._cache_dir,
            local_files_only=self.model_name == DEFAULT_MODEL,
        )

    async def _ensure_model(self) -> Any:
        """Load the model once, in a worker thread."""
        async with self._lock:
            if self._model is None:
                self._model = await asyncio.to_thread(self._load)
        return self._model

    async def dimension(self) -> int:
        """Return the model's vector dimension, measured once on a probe text."""
        if self._dimension is None:
            self._dimension = len(await self.embed_query(DIMENSION_PROBE))
        return self._dimension

    async def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """Embed *texts* in batches in a worker thread."""
        model = await self._ensure_model()
        return await asyncio.to_thread(
            lambda: [
                vector.tolist()
                for vector in model.embed(texts, batch_size=LOCAL_BATCH_SIZE)
            ]
        )

    async def embed_query(self, text: str) -> List[float]:
        """Embed one query, served from the LRU when seen recently."""
        return await self._queries.get(text, self._embed_query_uncached)

    async def _embed_query_uncached(self, text: str) -> List[float]:
        """Embed one query in a worker thread."""
        model = await self._ensure_model()
        return await asyncio.to_thread(
            lambda: next(iter(model.query_embed([text]))).tolist()
        )


class OpenAIEmbedder:
    """OpenAI-compatible `/embeddings` endpoint reached through llm_client."""

    def __init__(self, client: Any) -> None:
        self.model_name = client.model
        self._client = client
        self._dimension: Optional[int] = None
        self._queries = _QueryCache()

    async def dimension(self) -> int:
        """Return the endpoint's vector dimension, measured once on a probe text."""
        if self._dimension is None:
            self._dimension = len(await self.embed_query(DIMENSION_PROBE))
        return self._dimension

    async def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """Embed *texts* in batches."""
        vectors: List[List[float]] = []
        for start in range(0, len(texts), REMOTE_BATCH_SIZE):
            vectors.extend(
                await self._client.embed(texts[start : start + REMOTE_BATCH_SIZE])
            )
        return vectors

    async def embed_query(self, text: str) -> List[float]:
        """Embed one query, served from the LRU when seen recently."""
        return await self._queries.get(text, self._embed_query_uncached)

    async def _embed_query_uncached(self, text: str) -> List[float]:
        """Embed one query through the endpoint."""
        return (await self._client.embed([text]))[0]


def create_embedder(settings: VectorSearchSettings) -> Optional[Embedder]:
    """Return the embedder *settings* call for, or None when none is needed."""
    if not settings.uses_embedder:
        return None
    if settings.provider == PROVIDER_LOCAL:
        return LocalEmbedder(settings.model, settings.model_dir)
    if settings.provider == PROVIDER_OPENAI:
        from llm_client import create_embedding_client

        client = create_embedding_client(
            api_key=settings.embedding_api_key or "",
            model=settings.model,
            base_url=settings.embedding_base_url,
            timeout=settings.timeout_seconds,
        )
        return OpenAIEmbedder(client)
    return None
