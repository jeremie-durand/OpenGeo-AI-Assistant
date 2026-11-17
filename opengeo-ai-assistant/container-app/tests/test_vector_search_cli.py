"""CLI: idempotent builds, Parquet export reusable as an external index."""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from services.vector_search import cli
from services.vector_search.backends.base import SearchQuery
from services.vector_search.backends.parquet import ParquetBackend
from services.vector_search.index import VectorIndex
from services.vector_search.settings import VectorSearchSettings
from vector_search_fakes import FakeEmbedder, Sources


def managed_index(path: Path, src: Sources, embedder: FakeEmbedder) -> VectorIndex:
    return VectorIndex(
        VectorSearchSettings.from_env({}),
        backend=ParquetBackend(str(path), False, str(path.parent)),
        embedder=embedder,
        pc_source=src.pc_source,
        private_source=src.private_source,
        stac_api_url="",
    )


def test_second_build_reports_everything_unchanged(tmp_path: Path, capsys) -> None:
    src, embedder = Sources(), FakeEmbedder()
    factory = lambda: managed_index(tmp_path / "i.parquet", src, embedder)  # noqa: E731
    assert cli.main(["build"], factory) == 0
    first = capsys.readouterr().out
    embedder.embedded.clear()
    assert cli.main(["build"], factory) == 0
    second = capsys.readouterr().out
    assert "added=2" in first
    assert "unchanged=2 removed=0 embedded=0" in second
    assert embedder.embedded == []


def test_export_reopened_as_external_returns_same_results(tmp_path: Path) -> None:
    src, embedder = Sources(), FakeEmbedder()
    managed = managed_index(tmp_path / "i.parquet", src, embedder)
    out = tmp_path / "export.parquet"
    assert cli.main(["export", "--out", str(out)], lambda: managed) == 0

    settings = VectorSearchSettings.from_env(
        {"VECTOR_SEARCH_URL": f"file://{out}", "VECTOR_SEARCH_MODE": "external"}
    )
    external = VectorIndex(settings, embedder=embedder, stac_api_url="")
    query = SearchQuery("burn severity fire", 2)

    async def compare() -> Any:
        await external.start()
        await managed.start()
        return await managed.search(query), await external.search(query)

    expected, actual = asyncio.run(compare())
    assert external.status == "ready"
    assert [(h.id, h.score) for h in actual] == [(h.id, h.score) for h in expected]


def test_build_refuses_external_backend(capsys) -> None:
    settings = VectorSearchSettings.from_env({"VECTOR_SEARCH_URL": "https://h/search"})
    assert cli.main(["build"], lambda: VectorIndex(settings)) == 2
    assert "external" in capsys.readouterr().err


def test_status_prints_disabled(capsys) -> None:
    settings = VectorSearchSettings.from_env({"VECTOR_SEARCH_ENABLED": "false"})
    assert cli.main(["status"], lambda: VectorIndex(settings)) == 0
    assert '"status": "disabled"' in capsys.readouterr().out


def test_unknown_command_exits() -> None:
    with pytest.raises(SystemExit):
        cli.main(["nope"])


def test_status_does_not_build(tmp_path: Path, capsys) -> None:
    src, embedder = Sources(), FakeEmbedder()
    index = managed_index(tmp_path / "i.parquet", src, embedder)
    assert cli.main(["status"], lambda: index) == 0
    assert embedder.embedded == []
    assert '"status": "ready"' in capsys.readouterr().out
