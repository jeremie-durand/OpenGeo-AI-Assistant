"""MongoDB backend: `$vectorSearch` with numeric extent and date pre-filters."""

import inspect
from typing import Any, Callable, Collection, Dict, List, Optional, Sequence

from ..documents import (
    OPEN_END,
    OPEN_START,
    WORLD_BBOX,
    Bbox,
    Interval,
    mongo_dates,
    mongo_extent,
    normalize_bbox,
)
from .base import (
    BackendConfigError,
    BackendHit,
    BackendInfo,
    BackendNotReadyError,
    BackendUnavailableError,
    SearchQuery,
    StoredDocument,
    dimension_mismatch,
    query_lon_ranges,
)

FILTER_FIELDS = ("minx", "miny", "maxx", "maxy", "start_datetime", "end_datetime")
REQUIRED_FIELDS = ("id", "title", "description", "embedding") + FILTER_FIELDS
MIN_CANDIDATES = 100
CANDIDATE_FACTOR = 10
SEARCH_UNSUPPORTED = (
    "MongoDB $vectorSearch is unavailable: it needs MongoDB Atlas, or Community "
    "8.2+ with mongot."
)
CollectionFactory = Callable[[], Any]


def vector_filter(bbox: Optional[Bbox], interval: Optional[Interval]) -> Dict:
    """Return the `$vectorSearch` pre-filter for a bbox and time interval.

    Stored extents never cross the antimeridian (see `filter_extent`), so plain
    numeric ranges are enough; a crossing query bbox becomes an `$or`.
    """
    clauses: List[Dict[str, Any]] = []
    if bbox is not None:
        clauses += [{"miny": {"$lte": bbox[3]}}, {"maxy": {"$gte": bbox[1]}}]
        ranges = [
            {"$and": [{"minx": {"$lte": hi}}, {"maxx": {"$gte": lo}}]}
            for lo, hi in query_lon_ranges(bbox)
        ]
        clauses.append(ranges[0] if len(ranges) == 1 else {"$or": ranges})
    if interval is not None:
        start, end = interval
        if end is not None:
            clauses.append({"start_datetime": {"$lte": end}})
        if start is not None:
            clauses.append({"end_datetime": {"$gte": start}})
    return {"$and": clauses} if clauses else {}


def filter_extent(bbox: Optional[Bbox]) -> Bbox:
    """Return the extent stored in the filter fields.

    `$vectorSearch` filters cannot compare two fields, so an extent crossing the
    antimeridian is widened to every longitude: it may over-match, never miss.
    """
    minx, miny, maxx, maxy = mongo_extent(bbox)
    if minx > maxx:
        return (WORLD_BBOX[0], miny, WORLD_BBOX[2], maxy)
    return (minx, miny, maxx, maxy)


def index_definition(dimension: int) -> Dict[str, Any]:
    """Return the vector search index definition, with every filter field."""
    fields: List[Dict[str, Any]] = [
        {
            "type": "vector",
            "path": "embedding",
            "numDimensions": dimension,
            "similarity": "cosine",
        }
    ]
    fields += [{"type": "filter", "path": name} for name in FILTER_FIELDS]
    return {"fields": fields}


async def _to_list(cursor_or_awaitable: Any) -> List[Dict[str, Any]]:
    """Materialise a pymongo async cursor (possibly still awaitable)."""
    cursor = cursor_or_awaitable
    if inspect.isawaitable(cursor):
        cursor = await cursor
    return await cursor.to_list(length=None)


class MongoBackend:
    """One MongoDB collection searched through a vector search index."""

    def __init__(
        self,
        url: str,
        database: str,
        collection: str,
        index_name: str,
        read_only: bool,
        timeout: float,
        collection_factory: Optional[CollectionFactory] = None,
    ) -> None:
        self.read_only = read_only
        self._url = url
        self._database = database
        self._collection_name = collection
        self._index_name = index_name
        self._timeout = timeout
        self._factory = collection_factory
        self._collection: Any = None

    def _get_collection(self) -> Any:
        """Return the collection, creating the client on first use."""
        if self._collection is None:
            if self._factory is not None:
                self._collection = self._factory()
            else:
                from pymongo import AsyncMongoClient

                timeout_ms = int(self._timeout * 1000)
                client = AsyncMongoClient(
                    self._url,
                    tz_aware=True,
                    serverSelectionTimeoutMS=timeout_ms,
                    timeoutMS=timeout_ms,
                )
                self._collection = client[self._database][self._collection_name]
        return self._collection

    async def _call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Await a pymongo call, mapping driver errors."""
        from pymongo.errors import OperationFailure, PyMongoError

        try:
            result = func(*args, **kwargs)
            return await result if inspect.isawaitable(result) else result
        except OperationFailure as exc:
            if "vectorSearch" in str(exc) or "search index" in str(exc).lower():
                raise BackendConfigError(f"{SEARCH_UNSUPPORTED} ({exc})")
            raise BackendUnavailableError(f"MongoDB error: {exc}")
        except PyMongoError as exc:
            raise BackendUnavailableError(f"MongoDB is unreachable: {exc}")

    async def search(self, query: SearchQuery) -> List[BackendHit]:
        """Return the k nearest documents; scores are `(1 + cosine) / 2`."""
        if query.vector is None:
            raise BackendConfigError("The MongoDB backend needs a query vector.")
        stage: Dict[str, Any] = {
            "index": self._index_name,
            "path": "embedding",
            "queryVector": query.vector,
            "numCandidates": max(CANDIDATE_FACTOR * query.k, MIN_CANDIDATES),
            "limit": query.k,
        }
        search_filter = vector_filter(query.bbox, query.interval)
        if search_filter:
            stage["filter"] = search_filter
        pipeline = [
            {"$vectorSearch": stage},
            {
                "$project": {
                    "_id": 0,
                    "id": 1,
                    "title": 1,
                    "description": 1,
                    "score": {"$meta": "vectorSearchScore"},
                }
            },
        ]
        collection = self._get_collection()
        rows = await self._call(_to_list, collection.aggregate(pipeline))
        return [
            BackendHit(
                id=row["id"],
                title=row.get("title") or "",
                description=row.get("description") or "",
                score=float(row["score"]),
            )
            for row in rows
        ]

    async def stored(self) -> Dict[str, StoredDocument]:
        """Return every managed document."""
        self._require_writable()
        rows = await self._call(_to_list, self._get_collection().find({}))
        return {row["id"]: _row_to_document(row) for row in rows}

    async def upsert(self, documents: Sequence[StoredDocument]) -> None:
        """Insert or replace *documents*, keyed by id."""
        from pymongo import ReplaceOne

        self._require_writable()
        if not documents:
            return
        requests = [
            ReplaceOne({"_id": doc.id}, _document_to_row(doc), upsert=True)
            for doc in documents
        ]
        await self._call(self._get_collection().bulk_write, requests)

    async def delete_missing(self, keep_ids: Collection[str]) -> int:
        """Delete managed documents whose id is not in *keep_ids*."""
        self._require_writable()
        result = await self._call(
            self._get_collection().delete_many, {"id": {"$nin": list(keep_ids)}}
        )
        return int(result.deleted_count)

    def _require_writable(self) -> None:
        """Refuse writes in external mode."""
        if self.read_only:
            raise BackendConfigError("External MongoDB is read-only.")

    async def describe(self) -> BackendInfo:
        """Return document count, vector dimension and (managed) model."""
        collection = self._get_collection()
        count = await self._call(collection.count_documents, {})
        sample = await self._call(collection.find_one, {})
        if not sample:
            return BackendInfo(count=count)
        return BackendInfo(
            count=count,
            dimension=len(sample.get("embedding") or []) or None,
            model=None if self.read_only else sample.get("model"),
        )

    async def check(self, dimension: Optional[int]) -> None:
        """Ensure (managed) or validate (external) the vector search index."""
        if self.read_only:
            await self._check_fields()
        index = await self._find_index()
        if index is None:
            if self.read_only:
                raise BackendConfigError(
                    f"The vector search index '{self._index_name}' does not exist "
                    f"on '{self._collection_name}'."
                )
            await self._create_index(dimension)
            raise BackendNotReadyError("The MongoDB search index is being built.")
        indexed = _index_dimension(index)
        if dimension and indexed and indexed != dimension:
            if self.read_only:
                raise BackendConfigError(dimension_mismatch(indexed, dimension))
            await self._call(self._get_collection().drop_search_index, self._index_name)
            await self._create_index(dimension)
            raise BackendNotReadyError("The MongoDB search index is being rebuilt.")
        if not index.get("queryable", False):
            raise BackendNotReadyError("The MongoDB search index is not queryable yet.")

    async def _find_index(self) -> Optional[Dict[str, Any]]:
        """Return the configured search index description, if it exists."""
        collection = self._get_collection()
        indexes = await self._call(
            _to_list, collection.list_search_indexes(self._index_name)
        )
        return indexes[0] if indexes else None

    async def _create_index(self, dimension: Optional[int]) -> None:
        """Create the vector search index for *dimension*."""
        from pymongo.operations import SearchIndexModel

        if not dimension:
            raise BackendConfigError("The embedding dimension is unknown.")
        model = SearchIndexModel(
            definition=index_definition(dimension),
            name=self._index_name,
            type="vectorSearch",
        )
        await self._call(self._get_collection().create_search_index, model)

    async def _check_fields(self) -> None:
        """Check one external document carries every contract field."""
        sample = await self._call(self._get_collection().find_one, {})
        if sample is None:
            return
        missing = [name for name in REQUIRED_FIELDS if sample.get(name) is None]
        if missing:
            raise BackendConfigError(
                f"MongoDB documents in '{self._collection_name}' are missing the "
                f"fields: {', '.join(missing)}."
            )


def _index_dimension(index: Dict[str, Any]) -> Optional[int]:
    """Return numDimensions of the vector field in an index description."""
    definition = index.get("latestDefinition") or index.get("definition") or {}
    for field in definition.get("fields", []):
        if field.get("type") == "vector":
            return field.get("numDimensions")
    return None


def _document_to_row(doc: StoredDocument) -> Dict[str, Any]:
    """Return the MongoDB document for a stored row."""
    minx, miny, maxx, maxy = filter_extent(doc.bbox)
    start, end = mongo_dates(doc.start, doc.end)
    return {
        "_id": doc.id,
        "id": doc.id,
        "title": doc.title,
        "description": doc.description,
        "embedding": doc.embedding,
        "minx": minx,
        "miny": miny,
        "maxx": maxx,
        "maxy": maxy,
        "extent": list(doc.bbox) if doc.bbox else None,
        "start_datetime": start,
        "end_datetime": end,
        "model": doc.model,
        "content_hash": doc.content_hash,
        "source": doc.source,
    }


def _row_to_document(row: Dict[str, Any]) -> StoredDocument:
    """Build a stored document from a managed MongoDB document."""
    start = row.get("start_datetime")
    end = row.get("end_datetime")
    return StoredDocument(
        id=row["id"],
        title=row.get("title") or "",
        description=row.get("description") or "",
        embedding=list(row.get("embedding") or []),
        bbox=normalize_bbox(row.get("extent")),
        start=None if start is None or start == OPEN_START else start,
        end=None if end is None or end == OPEN_END else end,
        model=row.get("model") or "",
        content_hash=row.get("content_hash") or "",
        source=row.get("source") or "",
    )
