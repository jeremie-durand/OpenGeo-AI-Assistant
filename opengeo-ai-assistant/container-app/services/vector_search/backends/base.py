"""Backend protocol and the value types every vector backend shares."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Collection, Dict, List, Optional, Protocol, Sequence, Tuple

from ..documents import Bbox, Interval, lon_ranges

CONTRACT_COLUMNS = (
    "id",
    "title",
    "description",
    "embedding",
    "minx",
    "miny",
    "maxx",
    "maxy",
    "start_datetime",
    "end_datetime",
)


class BackendUnavailableError(RuntimeError):
    """A search or write could not reach the backend (or timed out)."""


class BackendConfigError(RuntimeError):
    """The backend is reachable but cannot serve the contract; str() is the reason."""


class BackendNotReadyError(RuntimeError):
    """The backend is being prepared (e.g. a search index is building)."""


@dataclass(frozen=True)
class SearchQuery:
    """One similarity search: text (always) and its vector (when embedded)."""

    text: str
    k: int
    kind: str = "collections"
    vector: Optional[List[float]] = None
    bbox: Optional[Bbox] = None
    interval: Optional[Interval] = None
    datetime_text: Optional[str] = None


@dataclass(frozen=True)
class BackendHit:
    """A ranked match as the backend returns it."""

    id: str
    score: float
    title: str = ""
    description: str = ""


@dataclass(frozen=True)
class StoredDocument:
    """A full stored row: contract fields plus managed bookkeeping."""

    id: str
    title: str
    description: str
    embedding: List[float] = field(repr=False)
    bbox: Optional[Bbox]
    start: Optional[datetime]
    end: Optional[datetime]
    model: str
    content_hash: str
    source: str

    @property
    def dimension(self) -> int:
        """Return the vector dimension of this row."""
        return len(self.embedding)


@dataclass(frozen=True)
class BackendInfo:
    """What a backend reports about its stored vectors."""

    count: Optional[int] = None
    dimension: Optional[int] = None
    model: Optional[str] = None


class VectorBackend(Protocol):
    """Storage and similarity search for collection documents."""

    read_only: bool

    async def search(self, query: SearchQuery) -> List[BackendHit]:
        """Return up to `query.k` hits, best first, scores higher-is-better."""
        ...

    async def stored(self) -> Dict[str, StoredDocument]:
        """Return every stored row by id (managed mode only)."""
        ...

    async def upsert(self, documents: Sequence[StoredDocument]) -> None:
        """Insert or replace *documents* (managed mode only)."""
        ...

    async def delete_missing(self, keep_ids: Collection[str]) -> int:
        """Delete rows whose id is not in *keep_ids*; return how many (managed)."""
        ...

    async def describe(self) -> BackendInfo:
        """Return the row count, vector dimension and model when known."""
        ...

    async def check(self, dimension: Optional[int]) -> None:
        """Raise BackendConfigError/BackendUnavailableError when unusable."""
        ...


def cosine_score(cosine: float) -> float:
    """Map a cosine similarity (-1..1) to the shared 0..1 score."""
    return (1.0 + cosine) / 2.0


def query_lon_ranges(bbox: Bbox) -> List[Tuple[float, float]]:
    """Return the longitude ranges of a query bbox (two across the antimeridian)."""
    return lon_ranges(bbox[0], bbox[2])


def dimension_mismatch(stored: int, expected: int) -> str:
    """Return the degraded reason for a stored/model dimension mismatch."""
    return (
        f"Stored vectors have {stored} dimensions but the embedding model "
        f"produces {expected}."
    )
