"""PostgreSQL backend: generated SQL, managed setup and external view checks."""

import asyncio
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import pytest
from services.vector_search.backends.base import (
    BackendConfigError,
    SearchQuery,
    StoredDocument,
)
from services.vector_search.backends.postgres import PostgresBackend

WRITE_RE = re.compile(r"^\s*(INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)", re.I)
VIEW_ROWS = [
    {"attname": name, "atttypmod": typmod}
    for name, typmod in [
        ("id", -1),
        ("title", -1),
        ("description", -1),
        ("embedding", 384),
        ("geom", -1),
        ("start_datetime", -1),
        ("end_datetime", -1),
    ]
]


class FakeConnection:
    """Records every statement and answers catalog queries from fixtures."""

    def __init__(
        self,
        attributes: Optional[List[Dict[str, Any]]] = None,
        sample_dims: Optional[int] = None,
        failing: Optional[str] = None,
    ) -> None:
        self.statements: List[Tuple[str, Tuple[Any, ...]]] = []
        self.attributes = VIEW_ROWS if attributes is None else attributes
        self.sample_dims = sample_dims
        self.failing = failing

    async def execute(self, sql: str, *args: Any) -> str:
        self.statements.append((sql, args))
        if self.failing and self.failing in sql:
            import asyncpg

            raise asyncpg.InsufficientPrivilegeError("permission denied")
        return "DELETE 2" if sql.startswith("DELETE") else "OK"

    async def executemany(self, sql: str, args: List[Tuple[Any, ...]]) -> None:
        self.statements.append((sql, tuple(args)))

    async def fetch(self, sql: str, *args: Any) -> List[Dict[str, Any]]:
        self.statements.append((sql, args))
        if "pg_attribute" in sql:
            return self.attributes
        if "score" in sql:
            return [{"id": "mtbs", "title": "MTBS", "description": "", "score": 0.9}]
        return []

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.statements.append((sql, args))
        return self.sample_dims

    async def fetchrow(self, sql: str, *args: Any) -> Dict[str, Any]:
        self.statements.append((sql, args))
        return {"count": 3, "dimension": 384, "model": None}


def make(
    connection: FakeConnection, read_only: bool = False, table: str = "geo.vectors"
) -> PostgresBackend:
    async def factory() -> FakeConnection:
        return connection

    return PostgresBackend("postgresql://db/geo", table, read_only, 5.0, factory)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_search_binds_every_value() -> None:
    connection = FakeConnection()
    query = SearchQuery(
        "burn",
        5,
        vector=[0.1, 0.2],
        bbox=(-79.8, 45.0, -57.1, 62.6),
        interval=(datetime(2001, 1, 1, tzinfo=timezone.utc), None),
    )
    hits = run(make(connection, read_only=True).search(query))
    sql, args = connection.statements[0]
    assert hits[0].id == "mtbs" and hits[0].score == 0.9
    assert "embedding <=> $1" in sql and "1 - (embedding <=> $1) / 2" in sql
    assert '"opengeo_search_collections"' in sql
    assert "ST_MakeEnvelope($3, $4, $5, $6, 4326)" in sql
    assert "end_datetime >= $7" in sql
    assert "-79.8" not in sql and "2001" not in sql
    assert args[:2] == ([0.1, 0.2], 5)


def test_search_splits_antimeridian_query_bbox() -> None:
    connection = FakeConnection()
    query = SearchQuery("q", 3, vector=[1.0], bbox=(170.0, -50.0, -170.0, -30.0))
    run(make(connection).search(query))
    sql, args = connection.statements[0]
    assert sql.count("ST_Intersects") == 2
    assert args[2:] == (170.0, -50.0, 180.0, -30.0, -180.0, -50.0, -170.0, -30.0)


def test_managed_setup_one_statement_per_extension() -> None:
    connection = FakeConnection()
    run(make(connection).check(384))
    statements = [sql for sql, _ in connection.statements]
    assert statements[:3] == [
        "CREATE EXTENSION IF NOT EXISTS vector",
        "CREATE EXTENSION IF NOT EXISTS postgis",
        'CREATE SCHEMA IF NOT EXISTS "geo"',
    ]
    assert statements[3].startswith('CREATE TABLE IF NOT EXISTS "geo"."vectors"')


def test_managed_setup_without_schema_creates_no_schema() -> None:
    connection = FakeConnection()
    run(make(connection, table="vectors").check(384))
    assert not any("SCHEMA" in sql for sql, _ in connection.statements)


def test_missing_extension_privilege_names_extension() -> None:
    connection = FakeConnection(failing="postgis")
    with pytest.raises(BackendConfigError, match="extension 'postgis'"):
        run(make(connection).check(384))


def test_upsert_and_delete_use_parameters() -> None:
    connection = FakeConnection()
    doc = StoredDocument(
        id="x'; DROP TABLE y; --",
        title="t",
        description="d",
        embedding=[1.0],
        bbox=(170.0, -50.0, -170.0, -30.0),
        start=None,
        end=None,
        model="m",
        content_hash="h",
        source="planetary_computer",
    )
    backend = make(connection)
    run(backend.upsert([doc]))
    removed = run(backend.delete_missing({"a"}))
    insert_sql, insert_args = connection.statements[0]
    delete_sql, delete_args = connection.statements[1]
    assert "DROP TABLE" not in insert_sql and "ST_Collect" in insert_sql
    assert insert_args[0][0] == doc.id
    assert delete_args == (["a"],) and removed == 2
    assert "ON CONFLICT (id) DO UPDATE" in insert_sql


def test_missing_view_names_view_and_columns() -> None:
    connection = FakeConnection(attributes=[])
    with pytest.raises(BackendConfigError) as error:
        run(make(connection, read_only=True).check(384))
    assert "opengeo_search_collections" in str(error.value)
    assert "start_datetime" in str(error.value)


def test_missing_view_column_is_named() -> None:
    rows = [row for row in VIEW_ROWS if row["attname"] != "geom"]
    with pytest.raises(BackendConfigError, match="missing the columns: geom"):
        run(make(FakeConnection(attributes=rows), read_only=True).check(384))


def test_dimension_mismatch_states_both() -> None:
    rows = [dict(row) for row in VIEW_ROWS]
    rows[3]["atttypmod"] = 1536
    with pytest.raises(BackendConfigError, match="1536 dimensions.*produces 384"):
        run(make(FakeConnection(attributes=rows), read_only=True).check(384))


def test_untyped_vector_column_reads_one_row() -> None:
    rows = [dict(row) for row in VIEW_ROWS]
    rows[3]["atttypmod"] = -1
    connection = FakeConnection(attributes=rows, sample_dims=1536)
    with pytest.raises(BackendConfigError, match="1536 dimensions.*produces 384"):
        run(make(connection, read_only=True).check(384))
    assert "vector_dims(embedding)" in connection.statements[-1][0]


def test_external_mode_never_writes() -> None:
    connection = FakeConnection()
    backend = make(connection, read_only=True)
    run(backend.check(384))
    run(backend.search(SearchQuery("q", 3, vector=[1.0])))
    run(backend.describe())
    with pytest.raises(BackendConfigError):
        run(backend.upsert([]))
    with pytest.raises(BackendConfigError):
        run(backend.delete_missing(set()))
    assert not any(WRITE_RE.match(sql) for sql, _ in connection.statements)


POSTGRES_URL = os.getenv("VECTOR_TEST_POSTGRES_URL", "")


@pytest.mark.skipif(not POSTGRES_URL, reason="VECTOR_TEST_POSTGRES_URL is not set")
def test_integration_managed_round_trip() -> None:
    table = f"opengeo_test.vectors_{uuid.uuid4().hex[:8]}"
    backend = PostgresBackend(POSTGRES_URL, table, False, 10.0)
    doc = StoredDocument(
        id="mtbs",
        title="MTBS",
        description="Burn severity",
        embedding=[1.0, 0.0, 0.0],
        bbox=(-125.0, 24.0, -66.9, 49.4),
        start=datetime(1984, 1, 1, tzinfo=timezone.utc),
        end=None,
        model="m",
        content_hash="h",
        source="planetary_computer",
    )

    async def scenario() -> None:
        await backend.check(3)
        await backend.upsert([doc])
        hits = await backend.search(SearchQuery("q", 3, vector=[1.0, 0.0, 0.0]))
        assert hits[0].id == "mtbs" and hits[0].score == pytest.approx(1.0)
        quebec = SearchQuery("q", 3, vector=[1.0, 0.0, 0.0], bbox=(-60, 50, -57, 52))
        assert await backend.search(quebec) == []
        assert (await backend.stored())["mtbs"].bbox == doc.bbox
        assert await backend.delete_missing(set()) == 1
        async with backend._connection() as connection:
            await connection.execute(f"DROP TABLE {backend.relation}")

    run(scenario())


@pytest.mark.skipif(not POSTGRES_URL, reason="VECTOR_TEST_POSTGRES_URL is not set")
def test_integration_external_view() -> None:
    table = f"opengeo_test.source_{uuid.uuid4().hex[:8]}"
    managed = PostgresBackend(POSTGRES_URL, table, False, 10.0)
    external = PostgresBackend(POSTGRES_URL, table, True, 10.0)
    doc = StoredDocument(
        id="ms-buildings",
        title="Building footprints",
        description="",
        embedding=[0.0, 1.0, 0.0],
        bbox=None,
        start=None,
        end=None,
        model="m",
        content_hash="h",
        source="planetary_computer",
    )

    async def scenario() -> None:
        await managed.check(3)
        await managed.upsert([doc])
        async with managed._connection() as connection:
            await connection.execute(
                "CREATE VIEW opengeo_search_collections AS SELECT id, title, "
                "description, embedding::vector(3) AS embedding, geom, "
                f"start_datetime, end_datetime FROM {managed.relation}"
            )
        try:
            await external.check(3)
            with pytest.raises(BackendConfigError, match="3 dimensions.*384"):
                await external.check(384)
            hits = await external.search(SearchQuery("q", 1, vector=[0.0, 1.0, 0.0]))
            assert [hit.id for hit in hits] == ["ms-buildings"]
        finally:
            async with managed._connection() as connection:
                await connection.execute("DROP VIEW opengeo_search_collections")
                await connection.execute(f"DROP TABLE {managed.relation}")

    run(scenario())
