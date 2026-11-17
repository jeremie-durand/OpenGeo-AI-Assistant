"""MongoDB backend: pipeline shape, index lifecycle, encoding and read-only mode."""

import asyncio
import os
import time
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from services.vector_search.backends.base import (
    BackendConfigError,
    BackendNotReadyError,
    SearchQuery,
    StoredDocument,
)
from services.vector_search.backends.mongodb import (
    MongoBackend,
    filter_extent,
    vector_filter,
)
from services.vector_search.documents import OPEN_END, OPEN_START, extent_intersects
from services.vector_search.settings import VectorSearchSettings

WRITES = {"bulk_write", "delete_many", "create_search_index", "drop_search_index"}


class FakeCursor:
    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self.rows = rows

    async def to_list(self, length: Optional[int] = None) -> List[Dict[str, Any]]:
        return self.rows


class FakeCollection:
    """Records calls by name and answers from canned rows and index states."""

    def __init__(
        self,
        indexes: Optional[List[Dict[str, Any]]] = None,
        sample: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.calls: List[tuple] = []
        self.indexes = indexes if indexes is not None else []
        self.sample = sample
        self.hits = [{"id": "mtbs", "title": "MTBS", "score": 0.93}]

    async def aggregate(self, pipeline: List[Dict[str, Any]]) -> FakeCursor:
        self.calls.append(("aggregate", pipeline))
        return FakeCursor(self.hits)

    async def list_search_indexes(self, name: str) -> FakeCursor:
        self.calls.append(("list_search_indexes", name))
        return FakeCursor(self.indexes)

    async def create_search_index(self, model: Any) -> str:
        self.calls.append(("create_search_index", model.document))
        return "id"

    async def drop_search_index(self, name: str) -> None:
        self.calls.append(("drop_search_index", name))

    async def bulk_write(self, requests: List[Any]) -> None:
        self.calls.append(("bulk_write", requests))

    async def delete_many(self, query: Dict[str, Any]) -> SimpleNamespace:
        self.calls.append(("delete_many", query))
        return SimpleNamespace(deleted_count=2)

    def find(self, query: Dict[str, Any]) -> FakeCursor:
        self.calls.append(("find", query))
        return FakeCursor([])

    async def find_one(self, query: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        self.calls.append(("find_one", query))
        return self.sample

    async def count_documents(self, query: Dict[str, Any]) -> int:
        self.calls.append(("count_documents", query))
        return 1


def index(dimension: int, queryable: bool = True) -> Dict[str, Any]:
    return {
        "name": "opengeo_vector_index",
        "queryable": queryable,
        "latestDefinition": {
            "fields": [{"type": "vector", "numDimensions": dimension}]
        },
    }


def make(collection: FakeCollection, read_only: bool = False) -> MongoBackend:
    return MongoBackend(
        "mongodb://db/geo",
        "geo",
        "opengeo_search_collections",
        "opengeo_vector_index",
        read_only,
        5.0,
        lambda: collection,
    )


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_pipeline_shape() -> None:
    collection = FakeCollection()
    query = SearchQuery(
        "burn",
        5,
        vector=[0.1, 0.2],
        bbox=(-79.8, 45.0, -57.1, 62.6),
        interval=(datetime(2001, 1, 1, tzinfo=timezone.utc), None),
    )
    hits = run(make(collection, read_only=True).search(query))
    stage = collection.calls[0][1][0]["$vectorSearch"]
    assert hits[0].id == "mtbs" and hits[0].score == 0.93
    assert stage["numCandidates"] == 100 and stage["limit"] == 5
    assert stage["index"] == "opengeo_vector_index"
    assert {"maxy": {"$gte": 45.0}} in stage["filter"]["$and"]
    assert {"end_datetime": {"$gte": query.interval[0]}} in stage["filter"]["$and"]
    project = collection.calls[0][1][1]["$project"]
    assert project["score"] == {"$meta": "vectorSearchScore"}


def test_num_candidates_scales_with_k() -> None:
    collection = FakeCollection()
    run(make(collection).search(SearchQuery("q", 36, vector=[1.0])))
    stage = collection.calls[0][1][0]["$vectorSearch"]
    assert stage["numCandidates"] == 360 and "filter" not in stage


def test_crossing_query_bbox_becomes_or() -> None:
    clauses = vector_filter((170.0, -50.0, -170.0, -30.0), None)["$and"]
    assert len(clauses[-1]["$or"]) == 2


def test_crossing_extent_is_widened_but_never_missed() -> None:
    pacific = (170.0, -50.0, -170.0, -30.0)
    stored = filter_extent(pacific)
    assert stored == (-180.0, -50.0, 180.0, -30.0)
    for query in [(-175.0, -45.0, -172.0, -40.0), (172.0, -45.0, 175.0, -40.0)]:
        assert extent_intersects(stored, query)


def test_upsert_encodes_empty_extent_and_dates() -> None:
    collection = FakeCollection()
    doc = StoredDocument(
        "ms-buildings", "B", "", [1.0], None, None, None, "m", "h", "planetary_computer"
    )
    run(make(collection).upsert([doc]))
    row = collection.calls[0][1][0]._doc
    assert (row["minx"], row["miny"], row["maxx"], row["maxy"]) == (-180, -90, 180, 90)
    assert (row["start_datetime"], row["end_datetime"]) == (OPEN_START, OPEN_END)
    assert row["extent"] is None and row["_id"] == "ms-buildings"


def test_delete_missing() -> None:
    collection = FakeCollection()
    assert run(make(collection).delete_missing({"a"})) == 2
    assert collection.calls[0] == ("delete_many", {"id": {"$nin": ["a"]}})


def test_managed_creates_missing_index_and_stays_building() -> None:
    collection = FakeCollection(indexes=[])
    with pytest.raises(BackendNotReadyError):
        run(make(collection).check(384))
    created = [c for c in collection.calls if c[0] == "create_search_index"][0][1]
    fields = created["definition"]["fields"]
    assert fields[0] == {
        "type": "vector",
        "path": "embedding",
        "numDimensions": 384,
        "similarity": "cosine",
    }
    assert {f["path"] for f in fields[1:]} >= {"minx", "end_datetime"}


def test_managed_index_not_queryable_keeps_building() -> None:
    with pytest.raises(BackendNotReadyError, match="not queryable"):
        run(make(FakeCollection(indexes=[index(384, queryable=False)])).check(384))


def test_managed_index_ready() -> None:
    run(make(FakeCollection(indexes=[index(384)])).check(384))


def test_managed_dimension_change_recreates_index() -> None:
    collection = FakeCollection(indexes=[index(1536)])
    with pytest.raises(BackendNotReadyError, match="rebuilt"):
        run(make(collection).check(384))
    names = [c[0] for c in collection.calls]
    assert names.index("drop_search_index") < names.index("create_search_index")


def test_external_missing_index_is_a_reason() -> None:
    with pytest.raises(BackendConfigError, match="'opengeo_vector_index' does not"):
        run(make(FakeCollection(indexes=[]), read_only=True).check(384))


def test_external_dimension_mismatch_states_both() -> None:
    collection = FakeCollection(indexes=[index(1536)])
    with pytest.raises(BackendConfigError, match="1536 dimensions.*produces 384"):
        run(make(collection, read_only=True).check(384))


def test_external_missing_fields_are_named() -> None:
    sample = {"id": "x", "title": "X", "description": "", "embedding": [1.0]}
    collection = FakeCollection(indexes=[index(1)], sample=sample)
    with pytest.raises(BackendConfigError, match="minx, miny"):
        run(make(collection, read_only=True).check(1))


def test_external_never_writes() -> None:
    collection = FakeCollection(indexes=[index(1536)])
    backend = make(collection, read_only=True)
    with pytest.raises(BackendConfigError):
        run(backend.check(384))
    run(backend.search(SearchQuery("q", 3, vector=[1.0])))
    run(backend.describe())
    for call in (backend.upsert([]), backend.delete_missing(set()), backend.stored()):
        with pytest.raises(BackendConfigError):
            run(call)
    assert not WRITES & {name for name, _ in collection.calls}


def test_database_from_url() -> None:
    settings = VectorSearchSettings.from_env({"VECTOR_SEARCH_URL": "mongodb://h/geo"})
    assert settings.mongo_database == "geo"


MONGODB_URL = os.getenv("VECTOR_TEST_MONGODB_URL", "")


@pytest.mark.skipif(not MONGODB_URL, reason="VECTOR_TEST_MONGODB_URL is not set")
def test_integration_managed_round_trip() -> None:
    name = f"vectors_{uuid.uuid4().hex[:8]}"
    backend = MongoBackend(
        MONGODB_URL, "opengeo_test", name, f"{name}_idx", False, 10.0
    )
    docs = [
        StoredDocument(
            "mtbs",
            "MTBS",
            "Burn severity",
            [1.0, 0.0, 0.0],
            (-125.0, 24.0, -66.9, 49.4),
            datetime(1984, 1, 1, tzinfo=timezone.utc),
            None,
            "m",
            "h1",
            "planetary_computer",
        ),
        StoredDocument(
            "global", "G", "", [0.9, 0.1, 0.0], None, None, None, "m", "h2", "p"
        ),
    ]

    async def wait_ready() -> None:
        deadline = time.monotonic() + 120
        while True:
            try:
                await backend.check(3)
                return
            except BackendNotReadyError:
                if time.monotonic() > deadline:
                    raise
                await asyncio.sleep(2)

    async def scenario() -> None:
        try:
            await backend.upsert(docs)
            await wait_ready()
            query = SearchQuery("q", 5, vector=[1.0, 0.0, 0.0])
            hits = await _eventually(lambda: backend.search(query), 2)
            assert hits[0].id == "mtbs" and hits[0].score == pytest.approx(1.0)
            quebec = SearchQuery(
                "q", 5, vector=[1.0, 0.0, 0.0], bbox=(-60.0, 50.0, -57.0, 52.0)
            )
            assert [h.id for h in await backend.search(quebec)] == ["global"]
            stored = await backend.stored()
            assert stored["global"].bbox is None and stored["global"].start is None
            assert stored["mtbs"].bbox == docs[0].bbox
            assert await backend.delete_missing({"global"}) == 1
        finally:
            await backend._get_collection().drop()

    run(scenario())


async def _eventually(search: Any, expected: int) -> List[Any]:
    """Retry a search until the index has caught up with recent writes."""
    deadline = time.monotonic() + 60
    while True:
        hits = await search()
        if len(hits) >= expected or time.monotonic() > deadline:
            return hits
        await asyncio.sleep(1)
