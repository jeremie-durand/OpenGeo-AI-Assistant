"""Parquet backend: similarity order, filters, writes and contract checks."""

import asyncio
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import duckdb
import pytest
from services.vector_search.backends.base import (
    BackendConfigError,
    BackendUnavailableError,
    SearchQuery,
    StoredDocument,
)
from services.vector_search.backends.parquet import ParquetBackend
from services.vector_search.documents import Bbox

QUEBEC: Bbox = (-79.8, 45.0, -57.1, 62.6)
TEXAS: Bbox = (-106.6, 25.8, -93.5, 36.5)
PACIFIC: Bbox = (170.0, -50.0, -170.0, -30.0)


def utc(year: int) -> datetime:
    return datetime(year, 1, 1, tzinfo=timezone.utc)


def stored(
    doc_id: str,
    vector: List[float],
    bbox: Optional[Bbox] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> StoredDocument:
    return StoredDocument(
        id=doc_id,
        title=doc_id.upper(),
        description=f"{doc_id} description",
        embedding=vector,
        bbox=bbox,
        start=start,
        end=end,
        model="fake-model",
        content_hash=f"hash-{doc_id}",
        source="planetary_computer",
    )


DOCS = [
    stored("exact", [1.0, 0.0, 0.0]),
    stored("close", [0.9, 0.1, 0.0], bbox=QUEBEC, start=utc(2015)),
    stored("far", [0.0, 1.0, 0.0], bbox=TEXAS, start=utc(1984), end=utc(2000)),
    stored("opposite", [-1.0, 0.0, 0.0], bbox=PACIFIC),
]


@pytest.fixture
def backend(tmp_path: Path) -> ParquetBackend:
    store = ParquetBackend(str(tmp_path / "index.parquet"), False, str(tmp_path))
    asyncio.run(store.upsert(DOCS))
    return store


def search(store: ParquetBackend, **kwargs) -> List[str]:
    query = SearchQuery(text="q", k=10, vector=[1.0, 0.0, 0.0], **kwargs)
    return [hit.id for hit in asyncio.run(store.search(query))]


def test_results_ordered_by_cosine_with_scores_in_unit_range(backend) -> None:
    hits = asyncio.run(backend.search(SearchQuery("q", 10, vector=[1.0, 0.0, 0.0])))
    assert [hit.id for hit in hits] == ["exact", "close", "far", "opposite"]
    assert hits[0].score == pytest.approx(1.0)
    assert hits[-1].score == pytest.approx(0.0, abs=1e-6)
    assert hits[0].title == "EXACT"


def test_k_limits_results(backend) -> None:
    query = SearchQuery("q", 2, vector=[1.0, 0.0, 0.0])
    assert len(asyncio.run(backend.search(query))) == 2


def test_bbox_filter_keeps_global_and_intersecting(backend) -> None:
    assert search(backend, bbox=(-72.0, 45.3, -71.8, 45.5)) == ["exact", "close"]


def test_bbox_filter_matches_extent_crossing_antimeridian(backend) -> None:
    west_side = search(backend, bbox=(-175.0, -45.0, -172.0, -40.0))
    east_side = search(backend, bbox=(172.0, -45.0, 175.0, -40.0))
    assert west_side == east_side == ["exact", "opposite"]
    assert search(backend, bbox=(0.0, -45.0, 10.0, -40.0)) == ["exact"]


def test_time_filter(backend) -> None:
    assert search(backend, interval=(utc(2001), utc(2002))) == ["exact", "opposite"]
    assert search(backend, interval=(None, utc(1990))) == ["exact", "far", "opposite"]


def test_upsert_then_delete_missing(backend) -> None:
    asyncio.run(backend.upsert([stored("new", [0.0, 0.0, 1.0])]))
    removed = asyncio.run(backend.delete_missing({"exact", "new"}))
    assert removed == 3
    assert set(asyncio.run(backend.stored())) == {"exact", "new"}


def test_upsert_replaces_by_id(backend) -> None:
    asyncio.run(backend.upsert([stored("far", [1.0, 0.0, 0.0])]))
    rows = asyncio.run(backend.stored())
    assert len(rows) == 4 and rows["far"].embedding == [1.0, 0.0, 0.0]


def test_stored_round_trip(backend) -> None:
    row = asyncio.run(backend.stored())["far"]
    assert row.bbox == TEXAS and row.end == utc(2000)
    assert (row.model, row.content_hash) == ("fake-model", "hash-far")


def test_describe(backend) -> None:
    info = asyncio.run(backend.describe())
    assert (info.count, info.dimension, info.model) == (4, 3, "fake-model")


def test_check_dimension_mismatch_states_both(backend) -> None:
    with pytest.raises(BackendConfigError, match="3 dimensions.*produces 384"):
        asyncio.run(backend.check(384))
    asyncio.run(backend.check(3))


def test_check_reports_missing_contract_columns(tmp_path: Path) -> None:
    path = tmp_path / "partial.parquet"
    duckdb.sql(f"COPY (SELECT 'a' AS id, [1.0] AS embedding) TO '{path}'")
    store = ParquetBackend(str(path), False, str(tmp_path))
    with pytest.raises(BackendConfigError, match="title, description"):
        asyncio.run(store.check(1))


def test_missing_managed_file_is_empty_but_not_searchable(tmp_path: Path) -> None:
    store = ParquetBackend(str(tmp_path / "none.parquet"), False, str(tmp_path))
    assert asyncio.run(store.stored()) == {}
    asyncio.run(store.check(3))
    with pytest.raises(BackendUnavailableError):
        asyncio.run(store.search(SearchQuery("q", 1, vector=[1.0])))


def test_remote_file_is_read_only(tmp_path: Path) -> None:
    store = ParquetBackend("s3://bucket/index.parquet", True, str(tmp_path))
    with pytest.raises(BackendConfigError, match="read-only"):
        asyncio.run(store.upsert(DOCS))
    with pytest.raises(BackendConfigError, match="read-only"):
        asyncio.run(store.delete_missing(set()))


def test_rewrite_is_atomic_for_concurrent_readers(backend, monkeypatch) -> None:
    """A reader during a rewrite sees the old or the new file, never a partial one."""
    real_replace = os.replace
    seen: List[int] = []

    def replace_after_read(src: str, dst: str) -> None:
        reader = threading.Thread(target=lambda: seen.append(_count(dst)))
        reader.start()
        reader.join()
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace_after_read)
    asyncio.run(backend.upsert([stored("new", [0.0, 0.0, 1.0])]))
    assert seen == [4]
    assert _count(backend.location) == 5
    leftovers = [p for p in os.listdir(os.path.dirname(backend.location))]
    assert leftovers == ["index.parquet"]


def _count(path: str) -> int:
    return duckdb.sql(f"SELECT count(*) FROM read_parquet('{path}')").fetchone()[0]


def test_remote_errors_never_show_url_secrets(tmp_path: Path) -> None:
    url = "https://user:secret@127.0.0.1:9/index.parquet?X-Amz-Signature=secret"
    store = ParquetBackend(url, True, "/opt/duckdb_extensions", timeout=10.0)
    with pytest.raises((BackendUnavailableError, BackendConfigError)) as error:
        asyncio.run(store.search(SearchQuery("q", 1, vector=[1.0])))
    assert "secret" not in str(error.value)
    assert "127.0.0.1" in str(error.value)
    assert error.value.__cause__ is None


def test_slow_read_times_out(backend, monkeypatch) -> None:
    import time as clock

    monkeypatch.setattr(backend, "_timeout", 0.1)
    monkeypatch.setattr(backend, "_search", lambda query: clock.sleep(1))
    with pytest.raises(BackendUnavailableError, match="timed out"):
        search(backend)


def test_wrong_query_dimension_is_a_config_error(backend) -> None:
    query = SearchQuery("q", 3, vector=[1.0, 0.0])
    with pytest.raises(BackendConfigError):
        asyncio.run(backend.search(query))
