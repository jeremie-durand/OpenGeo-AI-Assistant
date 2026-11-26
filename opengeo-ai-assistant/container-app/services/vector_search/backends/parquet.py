"""Parquet backend: DuckDB similarity search over a local or remote Parquet file."""

import asyncio
import os
import tempfile
from typing import Any, Collection, Dict, List, Optional, Sequence, Tuple

import duckdb

from ..documents import Interval
from ..settings import mask_url
from .base import (
    CONTRACT_COLUMNS,
    BackendConfigError,
    BackendHit,
    BackendInfo,
    BackendUnavailableError,
    SearchQuery,
    StoredDocument,
    dimension_mismatch,
    query_lon_ranges,
)

CONFIG_ERRORS = (
    duckdb.BinderException,
    duckdb.CatalogException,
    duckdb.ConversionException,
    duckdb.InvalidInputException,
)
MANAGED_COLUMNS = CONTRACT_COLUMNS + ("model", "content_hash", "source")
TABLE_DDL = """
CREATE TABLE docs (
    id VARCHAR, title VARCHAR, description VARCHAR, embedding FLOAT[],
    minx DOUBLE, miny DOUBLE, maxx DOUBLE, maxy DOUBLE,
    start_datetime TIMESTAMPTZ, end_datetime TIMESTAMPTZ,
    model VARCHAR, content_hash VARCHAR, source VARCHAR
)
"""
S3_SECRET_FIELDS = {
    "AWS_ACCESS_KEY_ID": "KEY_ID",
    "AWS_SECRET_ACCESS_KEY": "SECRET",
    "AWS_SESSION_TOKEN": "SESSION_TOKEN",
    "AWS_REGION": "REGION",
    "AWS_ENDPOINT_URL": "ENDPOINT",
}


def _literal(value: str) -> str:
    """Quote *value* as a SQL string literal."""
    return "'" + value.replace("'", "''") + "'"


def spatial_predicate(bbox: Any) -> Tuple[str, List[Any]]:
    """Return SQL and parameters matching extents (NULL = global) that hit *bbox*.

    Extents with minx > maxx cross the antimeridian and are matched as two
    longitude ranges.
    """
    if bbox is None:
        return "TRUE", []
    clauses, params = [], []
    for q_minx, q_maxx in query_lon_ranges(bbox):
        clauses.append(
            "((minx <= maxx AND minx <= ? AND maxx >= ?)"
            " OR (minx > maxx AND (minx <= ? OR maxx >= ?)))"
        )
        params.extend([q_maxx, q_minx, q_maxx, q_minx])
    sql = f"(minx IS NULL OR (miny <= ? AND maxy >= ? AND ({' OR '.join(clauses)})))"
    return sql, [bbox[3], bbox[1], *params]


def temporal_predicate(interval: Optional[Interval]) -> Tuple[str, List[Any]]:
    """Return SQL and parameters for time-range overlap (NULL = open)."""
    if interval is None:
        return "TRUE", []
    start, end = interval
    clauses, params = [], []
    if end is not None:
        clauses.append("(start_datetime IS NULL OR start_datetime <= ?)")
        params.append(end)
    if start is not None:
        clauses.append("(end_datetime IS NULL OR end_datetime >= ?)")
        params.append(start)
    return (" AND ".join(clauses) or "TRUE"), params


class ParquetBackend:
    """DuckDB over one Parquet file; remote files (s3, https) are read-only."""

    def __init__(
        self,
        location: str,
        remote: bool,
        extension_dir: str,
        s3: Optional[Dict[str, str]] = None,
        timeout: float = 5.0,
    ) -> None:
        self.location = location
        self.display_location = mask_url(location) if remote else location
        self.read_only = remote
        self._timeout = timeout
        self._extension_dir = extension_dir
        self._s3 = s3 or {}
        self._write_lock = asyncio.Lock()

    def _connect(self) -> duckdb.DuckDBPyConnection:
        """Open an in-memory connection, with httpfs and S3 secret when remote."""
        connection = duckdb.connect(config={"extension_directory": self._extension_dir})
        if self.read_only:
            connection.load_extension("httpfs")
            if self._s3:
                options = ", ".join(
                    f"{S3_SECRET_FIELDS[key]} {_literal(value)}"
                    for key, value in self._s3.items()
                )
                connection.execute(f"CREATE SECRET (TYPE s3, {options})")
        return connection

    def _exists(self) -> bool:
        """Return True when the file can be read (remote files are assumed so)."""
        return self.read_only or os.path.exists(self.location)

    def _source(self) -> str:
        """Return the read_parquet() expression for the file."""
        return f"read_parquet({_literal(self.location)})"

    async def _run(self, func: Any, *args: Any) -> Any:
        """Run blocking DuckDB work in a thread, bounded and with mapped errors."""
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(func, *args), timeout=self._timeout
            )
        except asyncio.TimeoutError:
            raise BackendUnavailableError(
                f"Reading {self.display_location} timed out."
            ) from None
        except CONFIG_ERRORS as exc:
            raise BackendConfigError(self._message("cannot be used", exc)) from None
        except duckdb.Error as exc:
            raise BackendUnavailableError(
                self._message("cannot be read", exc)
            ) from None

    def _message(self, problem: str, exc: BaseException) -> str:
        """Describe a DuckDB error; remote URLs and their errors stay masked."""
        detail = type(exc).__name__ if self.read_only else str(exc)
        return f"{self.display_location} {problem}: {detail}"

    async def search(self, query: SearchQuery) -> List[BackendHit]:
        """Return the k nearest documents by cosine similarity."""
        if query.vector is None:
            raise BackendConfigError("The Parquet backend needs a query vector.")
        if not self._exists():
            raise BackendUnavailableError(
                f"{self.display_location} does not exist yet."
            )
        return await self._run(self._search, query)

    def _search(self, query: SearchQuery) -> List[BackendHit]:
        """Blocking similarity search."""
        dim = len(query.vector or [])
        spatial, spatial_params = spatial_predicate(query.bbox)
        temporal, temporal_params = temporal_predicate(query.interval)
        sql = (
            "SELECT id, title, description, (1 + array_cosine_similarity("
            f"embedding::FLOAT[{dim}], ?::FLOAT[{dim}])) / 2 AS score "
            f"FROM {self._source()} WHERE {spatial} AND {temporal} "
            "ORDER BY score DESC LIMIT ?"
        )
        params = [query.vector, *spatial_params, *temporal_params, query.k]
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [
            BackendHit(id=r[0], title=r[1] or "", description=r[2] or "", score=r[3])
            for r in rows
        ]

    async def stored(self) -> Dict[str, StoredDocument]:
        """Return every row of the managed file, or nothing when it is absent."""
        if not self._exists():
            return {}
        return await self._run(self._stored)

    def _stored(self) -> Dict[str, StoredDocument]:
        """Blocking read of every row."""
        sql = f"SELECT {', '.join(MANAGED_COLUMNS)} FROM {self._source()}"
        with self._connect() as connection:
            rows = connection.execute(sql).fetchall()
        return {row[0]: _row_to_document(row) for row in rows}

    async def upsert(self, documents: Sequence[StoredDocument]) -> None:
        """Insert or replace *documents*, rewriting the file atomically."""
        self._require_writable()
        async with self._write_lock:
            current = await self.stored()
            current.update({doc.id: doc for doc in documents})
            await asyncio.to_thread(self._write, list(current.values()))

    async def delete_missing(self, keep_ids: Collection[str]) -> int:
        """Drop rows not in *keep_ids*, rewriting the file atomically."""
        self._require_writable()
        async with self._write_lock:
            current = await self.stored()
            kept = [doc for doc_id, doc in current.items() if doc_id in keep_ids]
            removed = len(current) - len(kept)
            if removed:
                await asyncio.to_thread(self._write, kept)
            return removed

    def _require_writable(self) -> None:
        """Refuse writes to remote files."""
        if self.read_only:
            raise BackendConfigError(f"{self.display_location} is read-only.")

    def _write(self, documents: List[StoredDocument]) -> None:
        """Write *documents* to a temporary file, then swap it in."""
        directory = os.path.dirname(self.location) or "."
        os.makedirs(directory, exist_ok=True)
        handle, temp_path = tempfile.mkstemp(suffix=".parquet", dir=directory)
        os.close(handle)
        try:
            with self._connect() as connection:
                connection.execute(TABLE_DDL)
                if documents:
                    connection.executemany(
                        f"INSERT INTO docs VALUES ({', '.join('?' * 13)})",
                        [_document_to_row(doc) for doc in documents],
                    )
                connection.execute(
                    f"COPY docs TO {_literal(temp_path)} (FORMAT parquet)"
                )
            os.replace(temp_path, self.location)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    async def describe(self) -> BackendInfo:
        """Return row count, dimension and model of the file."""
        if not self._exists():
            return BackendInfo(count=0)
        return await self._run(self._describe)

    def _describe(self) -> BackendInfo:
        """Blocking summary query."""
        columns = self._columns()
        model = "first(model)" if "model" in columns else "NULL"
        sql = f"SELECT count(*), max(len(embedding)), {model} FROM {self._source()}"
        with self._connect() as connection:
            count, dimension, stored_model = connection.execute(sql).fetchone()
        return BackendInfo(count=count, dimension=dimension, model=stored_model)

    def _columns(self) -> List[str]:
        """Return the file's column names."""
        with self._connect() as connection:
            rows = connection.execute(
                f"DESCRIBE SELECT * FROM {self._source()}"
            ).fetchall()
        return [row[0] for row in rows]

    async def check(self, dimension: Optional[int]) -> None:
        """Check the file is readable, has the contract columns and dimension."""
        if not self._exists():
            if self.read_only:
                raise BackendConfigError(f"{self.display_location} does not exist.")
            return
        columns = await self._run(self._columns)
        missing = [name for name in CONTRACT_COLUMNS if name not in columns]
        if missing:
            raise BackendConfigError(
                f"{self.display_location} is missing the columns: {', '.join(missing)}."
            )
        info = await self.describe()
        if dimension and info.dimension and info.dimension != dimension:
            raise BackendConfigError(dimension_mismatch(info.dimension, dimension))


def _document_to_row(doc: StoredDocument) -> Tuple[Any, ...]:
    """Flatten a stored document into the docs table column order."""
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


def _row_to_document(row: Sequence[Any]) -> StoredDocument:
    """Build a stored document from a docs table row."""
    bbox = None if row[4] is None else (row[4], row[5], row[6], row[7])
    return StoredDocument(
        id=row[0],
        title=row[1] or "",
        description=row[2] or "",
        embedding=list(row[3] or []),
        bbox=bbox,
        start=row[8],
        end=row[9],
        model=row[10] or "",
        content_hash=row[11] or "",
        source=row[12] or "",
    )
