"""Shared fakes for vector search tests: embedder and collection sources."""

import hashlib
from typing import Any, List, Set, Tuple

from services.vector_search import sources
from services.vector_search.documents import CollectionDocument

DIM = 8


class FakeEmbedder:
    """Deterministic bag-of-words vectors; counts what it embeds."""

    def __init__(self, model_name: str = "fake-model", dim: int = DIM) -> None:
        self.model_name = model_name
        self.dim = dim
        self.embedded: List[str] = []
        self.queries: List[str] = []

    def _vector(self, text: str) -> List[float]:
        vector = [0.0] * self.dim
        for word in text.lower().split():
            digest = hashlib.sha256(word.encode()).digest()
            vector[digest[0] % self.dim] += 1.0
        return vector

    async def dimension(self) -> int:
        return self.dim

    async def embed_documents(self, texts: List[str]) -> List[List[float]]:
        self.embedded += texts
        return [self._vector(t) for t in texts]

    async def embed_query(self, text: str) -> List[float]:
        self.queries.append(text)
        return self._vector(text)


def doc(doc_id: str, description: str, **kwargs: Any) -> CollectionDocument:
    return CollectionDocument(
        id=doc_id, title=doc_id, description=description, **kwargs
    )


class Sources:
    """Mutable fake sources for the Planetary Computer and private STAC."""

    def __init__(self) -> None:
        self.pc = [doc("mtbs", "burn severity fire"), doc("naip", "aerial imagery")]
        self.pc_reason = ""
        self.private = [doc("ortho", "aerial photos sherbrooke", source="private_stac")]
        self.private_renderable: Set[str] = {"ortho"}
        self.private_down = False
        self.private_calls: List[Set[str]] = []

    async def pc_source(self) -> Tuple[List[CollectionDocument], str]:
        return list(self.pc), self.pc_reason

    async def private_source(self, url: str, skip: Set[str]) -> sources.PrivateCatalog:
        self.private_calls.append(skip)
        if self.private_down:
            raise sources.SourceUnavailableError("Private STAC API is unavailable")
        return sources.PrivateCatalog(
            documents=[d for d in self.private if d.id not in skip],
            renderable_ids=set(self.private_renderable),
        )
