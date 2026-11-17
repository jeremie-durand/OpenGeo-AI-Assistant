"""VectorIndex: managed diff, model changes, status machine, check and refresh."""

import asyncio
import dataclasses
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from services.vector_search import index as vi
from services.vector_search.backends.base import (
    BackendNotReadyError,
    BackendUnavailableError,
    SearchQuery,
)
from services.vector_search.backends.parquet import ParquetBackend
from services.vector_search.settings import VectorSearchSettings
from vector_search_fakes import FakeEmbedder, Sources, doc


@pytest.fixture
def world(tmp_path: Path) -> Dict[str, Any]:
    src = Sources()
    embedder = FakeEmbedder()
    backend = ParquetBackend(str(tmp_path / "index.parquet"), False, str(tmp_path))
    index = vi.VectorIndex(
        VectorSearchSettings.from_env({}),
        backend=backend,
        embedder=embedder,
        pc_source=src.pc_source,
        private_source=src.private_source,
        stac_api_url="http://stac:8081",
    )
    return {"index": index, "src": src, "embedder": embedder, "backend": backend}


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_first_build_embeds_everything_and_is_ready(world) -> None:
    index = world["index"]
    run(index.start())
    assert index.status == vi.STATUS_READY
    assert len(world["embedder"].embedded) == 3
    assert index.count == 3
    assert "ortho" in index.renderable_ids and "mtbs" in index.renderable_ids


def test_second_build_reembeds_nothing(world) -> None:
    index = world["index"]
    run(index.build())
    world["embedder"].embedded.clear()
    report = run(index.build())
    assert world["embedder"].embedded == []
    assert (report.unchanged, report.added, report.updated, report.embedded) == (
        3,
        0,
        0,
        0,
    )


def test_changed_description_reembeds_one(world) -> None:
    index, src = world["index"], world["src"]
    run(index.build())
    world["embedder"].embedded.clear()
    src.pc[0] = doc("mtbs", "burn severity fire perimeters")
    report = run(index.build())
    assert len(world["embedder"].embedded) == 1
    assert report.updated == 1 and report.embedded == 1


def test_extents_arriving_later_are_stored_without_reembedding(world) -> None:
    index, src = world["index"], world["src"]
    run(index.build())
    world["embedder"].embedded.clear()
    src.pc[0] = doc("mtbs", "burn severity fire", bbox=(-125.0, 24.0, -66.0, 50.0))
    report = run(index.build())
    assert world["embedder"].embedded == []
    assert report.updated == 1
    assert run(world["backend"].stored())["mtbs"].bbox == (-125.0, 24.0, -66.0, 50.0)


def test_removed_collection_disappears(world) -> None:
    index, src = world["index"], world["src"]
    run(index.build())
    src.private = []
    report = run(index.build())
    assert report.removed == 1
    assert "ortho" not in run(world["backend"].stored())


def test_model_change_triggers_full_rebuild(world) -> None:
    index = world["index"]
    run(index.build())
    index._embedder = FakeEmbedder(model_name="other-model")
    report = run(index.build())
    assert report.embedded == 3
    assert {r.model for r in run(world["backend"].stored()).values()} == {"other-model"}


def test_private_ids_reusing_pc_ids_are_skipped(world) -> None:
    run(world["index"].build())
    assert world["src"].private_calls[0] == {"mtbs", "naip"}


def test_failed_private_refresh_keeps_previous_documents(world) -> None:
    index, src = world["index"], world["src"]
    run(index.build())
    src.private_down = True
    report = run(index.build())
    assert report.removed == 0
    assert "ortho" in run(world["backend"].stored())
    assert "ortho" in index.renderable_ids
    assert "unavailable" in index.last_error


class BrokenBackend:
    read_only = False

    def __init__(self) -> None:
        self.fail = True

    async def check(self, dimension: Optional[int]) -> None:
        if self.fail:
            raise BackendUnavailableError("PostgreSQL is unreachable")

    async def stored(self) -> Dict[str, Any]:
        return {}

    async def upsert(self, rows: Any) -> None:
        return None

    async def delete_missing(self, keep: Any) -> int:
        return 0

    async def describe(self) -> Any:
        return None

    async def search(self, query: SearchQuery) -> List[Any]:
        return []


def _index_with(backend: Any, src: Sources) -> vi.VectorIndex:
    return vi.VectorIndex(
        VectorSearchSettings.from_env({}),
        backend=backend,
        embedder=FakeEmbedder(),
        pc_source=src.pc_source,
        private_source=src.private_source,
        stac_api_url="",
    )


def test_build_error_is_degraded_with_reason() -> None:
    index = _index_with(BrokenBackend(), Sources())
    run(index.start())
    assert index.status == vi.STATUS_DEGRADED
    assert index.describe()["reason"] == "PostgreSQL is unreachable"


def test_degraded_build_is_retried_on_next_tick() -> None:
    backend = BrokenBackend()
    index = _index_with(backend, Sources())
    run(index.start())
    backend.fail = False
    run(index.tick())
    assert index.status == vi.STATUS_READY and index.built


def test_check_moves_ready_to_degraded_and_back(world) -> None:
    index = world["index"]
    run(index.start())
    backend = BrokenBackend()
    index._backend = backend
    run(index.tick())
    assert index.status == vi.STATUS_DEGRADED
    backend.fail = False
    run(index.tick())
    assert index.status == vi.STATUS_READY


def test_failed_refresh_keeps_last_good_index(world, monkeypatch) -> None:
    index = world["index"]
    run(index.start())
    index.last_build_at -= vi.REFRESH_INTERVAL_SECONDS

    async def broken_source() -> Any:
        raise RuntimeError("catalog exploded")

    index._pc_source = broken_source
    run(index.tick())
    assert index.status == vi.STATUS_READY
    assert "catalog exploded" in index.last_error
    assert len(run(world["backend"].stored())) == 3


def test_not_ready_backend_is_building() -> None:
    class Building(BrokenBackend):
        async def check(self, dimension: Optional[int]) -> None:
            raise BackendNotReadyError("The MongoDB search index is being built.")

    index = _index_with(Building(), Sources())
    run(index.start())
    assert index.status == vi.STATUS_BUILDING and index.built


def test_disabled_and_config_error_never_build() -> None:
    disabled = vi.VectorIndex(
        VectorSearchSettings.from_env({"VECTOR_SEARCH_ENABLED": "false"})
    )
    run(disabled.start())
    assert disabled.status == vi.STATUS_DISABLED
    assert disabled._backend is None and disabled._embedder is None
    broken = vi.VectorIndex(
        VectorSearchSettings.from_env({"VECTOR_SEARCH_URL": "ftp://x"})
    )
    run(broken.start())
    assert broken.status == vi.STATUS_DEGRADED and "Supported schemes" in broken.reason


def test_search_requires_ready_and_embeds_query(world) -> None:
    index = world["index"]
    with pytest.raises(BackendUnavailableError, match="still building"):
        run(index.search(SearchQuery("burn severity", 2)))
    run(index.start())
    hits = run(index.search(SearchQuery("burn severity fire", 2)))
    assert hits[0].id == "mtbs"
    assert world["embedder"].queries == ["burn severity fire"]


def test_search_is_bounded_by_the_timeout(world, monkeypatch) -> None:
    index = world["index"]
    run(index.start())

    async def slow(query: SearchQuery) -> List[Any]:
        await asyncio.sleep(1)
        return []

    monkeypatch.setattr(world["backend"], "search", slow)
    index.settings = dataclasses.replace(index.settings, timeout_seconds=0.05)
    with pytest.raises(asyncio.TimeoutError):
        run(index.search(SearchQuery("q", 1)))


def test_describe_masks_credentials() -> None:
    settings = VectorSearchSettings.from_env(
        {"VECTOR_SEARCH_URL": "postgresql://user:secret@db:5432/geo"}
    )
    assert "secret" not in str(vi.VectorIndex(settings).describe())


def test_get_index_is_a_singleton() -> None:
    vi.reset_index()
    assert vi.get_index() is vi.get_index()
    vi.reset_index()


def test_both_source_errors_are_reported(world) -> None:
    index, src = world["index"], world["src"]
    src.pc_reason = "Planetary Computer extents unavailable"
    src.private_down = True
    run(index.build())
    assert "Planetary Computer" in index.last_error
    assert "Private STAC" in index.last_error


def test_restart_with_private_stac_down_keeps_private_ids(world, tmp_path) -> None:
    run(world["index"].build())
    src = world["src"]
    src.private_down = True
    restarted = vi.VectorIndex(
        VectorSearchSettings.from_env({}),
        backend=world["backend"],
        embedder=FakeEmbedder(),
        pc_source=src.pc_source,
        private_source=src.private_source,
        stac_api_url="http://stac:8081",
    )
    run(restarted.start())
    assert "ortho" in restarted.private_ids
    assert "ortho" not in restarted.renderable_ids
    assert "ortho" in run(world["backend"].stored())
