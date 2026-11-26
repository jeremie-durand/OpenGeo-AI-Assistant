"""PostgreSQL backend: pgvector similarity with PostGIS extent filters."""

import asyncio
import re
from contextlib import asynccontextmanager
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Collection,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
)

from ..documents import Interval
from .base import (
    BackendConfigError,
    BackendHit,
    BackendInfo,
    BackendUnavailableError,
    SearchQuery,
    StoredDocument,
    dimension_mismatch,
    query_lon_ranges,
)

EXTERNAL_VIEW = "opengeo_search_collections"
VIEW_COLUMNS = (
    "id",
    "title",
    "description",
    "embedding",
    "geom",
    "start_datetime",
    "end_datetime",
)
EXTENSIONS = ("vector", "postgis")
STORED_COLUMNS = (
    "id, title, description, embedding, minx, miny, maxx, maxy, "
    "start_datetime, end_datetime, model, content_hash, source"
)
GEOM_FROM_EXTENT = (
    "CASE WHEN $5::float8 IS NULL THEN NULL "
    "WHEN $5::float8 <= $7::float8 "
    "THEN ST_MakeEnvelope($5, $6, $7, $8, 4326) "
    "ELSE ST_Multi(ST_Collect(ST_MakeEnvelope($5, $6, 180, $8, 4326), "
    "ST_MakeEnvelope(-180, $6, $7, $8, 4326))) END"
)
DELETE_COUNT_RE = re.compile(r"DELETE (\d+)")
ConnectionFactory = Callable[[], Awaitable[Any]]


def quote_identifier(name: str) -> str:
    """Quote a `table` or `schema.table` name part by part."""
    return ".".join('"' + part.replace('"', '""') + '"' for part in name.split("."))


def spatial_sql(bbox: Any, first_param: int) -> Tuple[str, List[Any]]:
    """Return an ST_Intersects predicate (NULL geom = global) and its parameters."""
    if bbox is None:
        return "TRUE", []
    clauses, params = [], []
    index = first_param
    for q_minx, q_maxx in query_lon_ranges(bbox):
        clauses.append(
            f"ST_Intersects(geom, ST_MakeEnvelope(${index}, ${index + 1}, "
            f"${index + 2}, ${index + 3}, 4326))"
        )
        params.extend([q_minx, bbox[1], q_maxx, bbox[3]])
        index += 4
    return f"(geom IS NULL OR {' OR '.join(clauses)})", params


def temporal_sql(interval: Optional[Interval], first_param: int) -> Tuple[str, List]:
    """Return a time-overlap predicate (NULL = open) and its parameters."""
    if interval is None:
        return "TRUE", []
    start, end = interval
    clauses, params = [], []
    index = first_param
    if end is not None:
        clauses.append(f"(start_datetime IS NULL OR start_datetime <= ${index})")
        params.append(end)
        index += 1
    if start is not None:
        clauses.append(f"(end_datetime IS NULL OR end_datetime >= ${index})")
        params.append(start)
    return (" AND ".join(clauses) or "TRUE"), params


class PostgresBackend:
    """pgvector + PostGIS; managed mode owns one table, external reads a view."""

    def __init__(
        self,
        url: str,
        table: str,
        read_only: bool,
        timeout: float,
        connection_factory: Optional[ConnectionFactory] = None,
    ) -> None:
        self.read_only = read_only
        self._url = url
        self._table = table
        self._timeout = timeout
        self._factory = connection_factory
        self._pool: Any = None
        self._pool_lock = asyncio.Lock()

    @property
    def relation(self) -> str:
        """Return the quoted relation searches read from."""
        return quote_identifier(EXTERNAL_VIEW if self.read_only else self._table)

    async def _create_pool(self) -> Any:
        """Create the asyncpg pool with the pgvector codec registered."""
        import asyncpg
        from pgvector.asyncpg import register_vector

        return await asyncpg.create_pool(
            self._url,
            min_size=0,
            max_size=4,
            timeout=self._timeout,
            command_timeout=self._timeout,
            init=register_vector,
        )

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[Any]:
        """Yield a connection, mapping connectivity errors to unavailable."""
        try:
            if self._factory is not None:
                yield await self._factory()
                return
            async with self._pool_lock:
                if self._pool is None:
                    self._pool = await self._create_pool()
            async with self._pool.acquire() as connection:
                yield connection
        except (OSError, asyncio.TimeoutError) as exc:
            raise BackendUnavailableError(f"PostgreSQL is unreachable: {exc}")
        except ValueError as exc:
            if "unknown type" not in str(exc):
                raise
            raise BackendConfigError(
                "The PostgreSQL 'vector' extension is not installed in this "
                "database (pgvector is required)."
            )
        except Exception as exc:
            if _is_postgres_error(exc):
                raise BackendUnavailableError(f"PostgreSQL error: {exc}")
            raise

    @asynccontextmanager
    async def _setup_connection(self) -> AsyncIterator[Any]:
        """Yield a codec-free connection, usable before the extensions exist."""
        if self._factory is not None:
            yield await self._factory()
            return
        import asyncpg

        try:
            connection = await asyncpg.connect(self._url, timeout=self._timeout)
        except (OSError, asyncio.TimeoutError) as exc:
            raise BackendUnavailableError(f"PostgreSQL is unreachable: {exc}")
        except Exception as exc:
            if _is_postgres_error(exc):
                raise BackendUnavailableError(f"PostgreSQL error: {exc}")
            raise
        try:
            yield connection
        finally:
            await connection.close()

    async def search(self, query: SearchQuery) -> List[BackendHit]:
        """Return the k nearest rows by cosine distance."""
        if query.vector is None:
            raise BackendConfigError("The PostgreSQL backend needs a query vector.")
        spatial, spatial_params = spatial_sql(query.bbox, 3)
        temporal, temporal_params = temporal_sql(
            query.interval, 3 + len(spatial_params)
        )
        sql = (
            "SELECT id, title, description, 1 - (embedding <=> $1) / 2 AS score "
            f"FROM {self.relation} WHERE {spatial} AND {temporal} "
            "ORDER BY embedding <=> $1 LIMIT $2"
        )
        params = [query.vector, query.k, *spatial_params, *temporal_params]
        async with self._connection() as connection:
            rows = await connection.fetch(sql, *params)
        return [
            BackendHit(
                id=row["id"],
                title=row["title"] or "",
                description=row["description"] or "",
                score=float(row["score"]),
            )
            for row in rows
        ]

    async def stored(self) -> Dict[str, StoredDocument]:
        """Return every row of the managed table."""
        self._require_writable()
        async with self._connection() as connection:
            rows = await connection.fetch(
                f"SELECT {STORED_COLUMNS} FROM {self.relation}"
            )
        return {row["id"]: _row_to_document(row) for row in rows}

    async def upsert(self, documents: Sequence[StoredDocument]) -> None:
        """Insert or replace *documents* in the managed table."""
        self._require_writable()
        if not documents:
            return
        sql = (
            f"INSERT INTO {self.relation} (id, title, description, embedding, "
            "minx, miny, maxx, maxy, geom, start_datetime, end_datetime, model, "
            f"content_hash, source) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, "
            f"{GEOM_FROM_EXTENT}, $9, $10, $11, $12, $13) "
            "ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title, "
            "description = EXCLUDED.description, embedding = EXCLUDED.embedding, "
            "minx = EXCLUDED.minx, miny = EXCLUDED.miny, maxx = EXCLUDED.maxx, "
            "maxy = EXCLUDED.maxy, geom = EXCLUDED.geom, "
            "start_datetime = EXCLUDED.start_datetime, "
            "end_datetime = EXCLUDED.end_datetime, model = EXCLUDED.model, "
            "content_hash = EXCLUDED.content_hash, source = EXCLUDED.source"
        )
        async with self._connection() as connection:
            await connection.executemany(sql, [_document_args(d) for d in documents])

    async def delete_missing(self, keep_ids: Collection[str]) -> int:
        """Delete managed rows whose id is not in *keep_ids*."""
        self._require_writable()
        async with self._connection() as connection:
            status = await connection.execute(
                f"DELETE FROM {self.relation} WHERE NOT (id = ANY($1::text[]))",
                list(keep_ids),
            )
        match = DELETE_COUNT_RE.search(status or "")
        return int(match.group(1)) if match else 0

    def _require_writable(self) -> None:
        """Refuse writes in external mode."""
        if self.read_only:
            raise BackendConfigError("External PostgreSQL is read-only.")

    async def describe(self) -> BackendInfo:
        """Return row count, vector dimension and (managed) model."""
        model = "NULL" if self.read_only else "min(model)"
        async with self._connection() as connection:
            row = await connection.fetchrow(
                "SELECT count(*) AS count, max(vector_dims(embedding)) AS dimension, "
                f"{model} AS model FROM {self.relation}"
            )
        return BackendInfo(
            count=row["count"], dimension=row["dimension"], model=row["model"]
        )

    async def check(self, dimension: Optional[int]) -> None:
        """Prepare the managed table, or validate the external view."""
        if self.read_only:
            await self._check_view(dimension)
        else:
            await self._prepare_managed()

    async def _prepare_managed(self) -> None:
        """Create the extensions, schema and table the managed mode needs."""
        async with self._setup_connection() as connection:
            for extension in EXTENSIONS:
                await _create_extension(connection, extension)
            if "." in self._table:
                schema = quote_identifier(self._table.split(".", 1)[0])
                await connection.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
            await connection.execute(
                f"CREATE TABLE IF NOT EXISTS {self.relation} ("
                "id text PRIMARY KEY, title text, description text, "
                "embedding vector, minx float8, miny float8, maxx float8, "
                "maxy float8, geom geometry(Geometry, 4326), "
                "start_datetime timestamptz, end_datetime timestamptz, "
                "model text, content_hash text, source text)"
            )

    async def _check_view(self, dimension: Optional[int]) -> None:
        """Validate the external view's columns and vector dimension."""
        async with self._connection() as connection:
            rows = await connection.fetch(
                "SELECT attname, atttypmod FROM pg_attribute "
                "WHERE attrelid = to_regclass($1) AND attnum > 0 "
                "AND NOT attisdropped",
                EXTERNAL_VIEW,
            )
            if not rows:
                raise BackendConfigError(
                    f"The view or table '{EXTERNAL_VIEW}' does not exist. It must "
                    f"expose the columns: {', '.join(VIEW_COLUMNS)}."
                )
            columns = {row["attname"]: row["atttypmod"] for row in rows}
            missing = [name for name in VIEW_COLUMNS if name not in columns]
            if missing:
                raise BackendConfigError(
                    f"'{EXTERNAL_VIEW}' is missing the columns: {', '.join(missing)}."
                )
            stored = columns["embedding"]
            if stored is None or stored < 1:
                stored = await connection.fetchval(
                    f"SELECT vector_dims(embedding) FROM {self.relation} LIMIT 1"
                )
        if dimension and stored and stored != dimension:
            raise BackendConfigError(dimension_mismatch(stored, dimension))


async def _create_extension(connection: Any, extension: str) -> None:
    """Create *extension*, naming it in the reason when that is not allowed."""
    try:
        await connection.execute(f"CREATE EXTENSION IF NOT EXISTS {extension}")
    except Exception as exc:
        if not _is_postgres_error(exc):
            raise
        raise BackendConfigError(
            f"Cannot create the PostgreSQL extension '{extension}': {exc}. "
            f"Ask a database administrator to run: CREATE EXTENSION {extension};"
        )


def _is_postgres_error(exc: Exception) -> bool:
    """Return True for asyncpg server and interface errors."""
    try:
        import asyncpg
    except ImportError:
        return False
    return isinstance(exc, (asyncpg.PostgresError, asyncpg.InterfaceError))


def _document_args(doc: StoredDocument) -> Tuple[Any, ...]:
    """Return the INSERT parameters of one stored document."""
    minx, miny, maxx, maxy = doc.bbox if doc.bbox else (None, None, None, None)
    return (
        doc.id,
        doc.title,
        doc.description,
        doc.embedding,
        minx,
        miny,
        maxx,
        maxy,
        doc.start,
        doc.end,
        doc.model,
        doc.content_hash,
        doc.source,
    )


def _row_to_document(row: Any) -> StoredDocument:
    """Build a stored document from a managed table row."""
    bbox = None
    if row["minx"] is not None:
        bbox = (row["minx"], row["miny"], row["maxx"], row["maxy"])
    embedding = row["embedding"]
    vector = embedding.to_list() if hasattr(embedding, "to_list") else embedding
    return StoredDocument(
        id=row["id"],
        title=row["title"] or "",
        description=row["description"] or "",
        embedding=list(vector or []),
        bbox=bbox,
        start=row["start_datetime"],
        end=row["end_datetime"],
        model=row["model"] or "",
        content_hash=row["content_hash"] or "",
        source=row["source"] or "",
    )
