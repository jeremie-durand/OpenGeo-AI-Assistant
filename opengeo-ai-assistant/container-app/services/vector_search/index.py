"""VectorIndex: builds, checks and refreshes the collection index in the background."""

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

from services import dashboard_catalog

from . import sources
from .backends.base import (
    BackendConfigError,
    BackendHit,
    BackendNotReadyError,
    BackendUnavailableError,
    SearchQuery,
    StoredDocument,
    VectorBackend,
)
from .documents import SOURCE_PRIVATE_STAC, CollectionDocument
from .embedders import Embedder, create_embedder
from .settings import (
    BACKEND_HTTP,
    BACKEND_MONGODB,
    BACKEND_PARQUET,
    BACKEND_POSTGRES,
    MODE_EXTERNAL,
    VectorSearchSettings,
)

logger = logging.getLogger(__name__)

STATUS_DISABLED = "disabled"
STATUS_BUILDING = "building"
STATUS_READY = "ready"
STATUS_DEGRADED = "degraded"
CHECK_INTERVAL_SECONDS = 5 * 60
REFRESH_INTERVAL_SECONDS = 24 * 60 * 60

PcSource = Callable[[], Awaitable[Tuple[List[CollectionDocument], str]]]
PrivateSource = Callable[[str, Set[str]], Awaitable[sources.PrivateCatalog]]


@dataclass
class BuildReport:
    """What one managed build changed."""

    added: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    embedded: int = 0


def create_backend(settings: VectorSearchSettings) -> VectorBackend:
    """Return the backend *settings* select."""
    read_only = settings.mode == MODE_EXTERNAL
    if settings.backend == BACKEND_PARQUET:
        from .backends.parquet import ParquetBackend

        return ParquetBackend(
            settings.location,
            settings.remote,
            settings.extension_dir,
            settings.s3,
            settings.timeout_seconds,
        )
    if settings.backend == BACKEND_POSTGRES:
        from .backends.postgres import PostgresBackend

        return PostgresBackend(
            settings.url, settings.table, read_only, settings.timeout_seconds
        )
    if settings.backend == BACKEND_MONGODB:
        from .backends.mongodb import MongoBackend

        return MongoBackend(
            settings.url,
            settings.mongo_database,
            settings.mongo_collection,
            settings.mongo_index,
            read_only,
            settings.timeout_seconds,
        )
    if settings.backend == BACKEND_HTTP:
        from .backends.http import HttpRetrieverBackend

        return HttpRetrieverBackend(
            settings.url, settings.api_key, settings.timeout_seconds
        )
    raise BackendConfigError(f"Unknown backend '{settings.backend}'.")


class VectorIndex:
    """Owns the backend, embedder, renderable set and status of vector search."""

    def __init__(
        self,
        settings: VectorSearchSettings,
        backend: Optional[VectorBackend] = None,
        embedder: Optional[Embedder] = None,
        pc_source: Optional[PcSource] = None,
        private_source: Optional[PrivateSource] = None,
        stac_api_url: Optional[str] = None,
    ) -> None:
        self.settings = settings
        self.status = STATUS_DISABLED if not settings.enabled else STATUS_BUILDING
        self.reason: Optional[str] = None
        self.last_error: Optional[str] = None
        self.count: Optional[int] = None
        self.built = False
        self.last_build_at = 0.0
        self.private_renderable: Set[str] = set()
        self.private_ids: Set[str] = set()
        self._private_documents: Dict[str, CollectionDocument] = {}
        self._backend = backend
        self._embedder = embedder
        self._pc_source = pc_source or sources.planetary_computer_documents
        self._private_source = private_source or sources.private_stac_catalog
        self._stac_api_url = (
            stac_api_url
            if stac_api_url is not None
            else os.getenv("STAC_API_URL", "").strip()
        )

    @property
    def ready(self) -> bool:
        """Return True when searches may be served."""
        return self.status == STATUS_READY

    @property
    def renderable_ids(self) -> Set[str]:
        """Return every collection id the map can render."""
        return set(dashboard_catalog.renderable_collection_ids()) | set(
            self.private_renderable
        )

    def describe(self) -> Dict[str, Any]:
        """Return the health-check view of vector search (credentials masked)."""
        info: Dict[str, Any] = {
            "status": self.status,
            "backend": self.settings.backend,
            "mode": self.settings.mode,
            "url": self.settings.display_url,
        }
        if self.settings.uses_embedder:
            info["embedding_model"] = self.settings.model
        if self.count is not None:
            info["document_count"] = self.count
        if self.reason and self.status == STATUS_DEGRADED:
            info["reason"] = self.reason
        if self.last_error:
            info["last_error"] = self.last_error
        return info

    def _set_status(self, status: str, reason: Optional[str] = None) -> None:
        """Move to *status*, logging transitions."""
        if status != self.status or reason != self.reason:
            logger.info(f"[VS] vector search {status}{f': {reason}' if reason else ''}")
        self.status = status
        self.reason = reason

    def _ensure_components(self) -> None:
        """Create the backend and embedder on first use."""
        if self._backend is None:
            self._backend = create_backend(self.settings)
        if self._embedder is None:
            self._embedder = create_embedder(self.settings)

    async def _dimension(self) -> Optional[int]:
        """Return the embedder's dimension, or None without an embedder."""
        return await self._embedder.dimension() if self._embedder else None

    async def start(self) -> None:
        """Run the first build or check, setting the status; never raises."""
        if not self.settings.enabled:
            self._set_status(STATUS_DISABLED)
            return
        if self.settings.error:
            self._set_status(STATUS_DEGRADED, self.settings.error)
            return
        await self._attempt(self.build)

    async def _attempt(self, action: Callable[[], Awaitable[Any]]) -> bool:
        """Run a build or check and map its outcome to a status."""
        try:
            await action()
        except BackendNotReadyError as exc:
            self._set_status(STATUS_BUILDING, str(exc))
            return False
        except Exception as exc:
            logger.warning(
                f"[VS] {action.__name__} failed: {type(exc).__name__}: {_reason(exc)}"
            )
            self._set_status(STATUS_DEGRADED, _reason(exc))
            return False
        self._set_status(STATUS_READY)
        return True

    async def build(self) -> Optional[BuildReport]:
        """Build (managed) or validate (external) the index."""
        self._ensure_components()
        dimension = await self._dimension()
        if self.settings.mode == MODE_EXTERNAL:
            await self._backend.check(dimension)
            self.last_error = await self._refresh_private(skip=set())
            self.count = (await self._backend.describe()).count
            self.built = True
            self.last_build_at = time.monotonic()
            return None
        not_ready: Optional[BackendNotReadyError] = None
        try:
            await self._backend.check(dimension)
        except BackendNotReadyError as exc:
            not_ready = exc
        report = await self._build_managed(dimension or 0)
        self.built = True
        self.last_build_at = time.monotonic()
        if not_ready:
            raise not_ready
        return report

    async def _build_managed(self, dimension: int) -> BuildReport:
        """Diff sources against stored rows; embed, upsert and delete the changes."""
        pc_documents, pc_reason = await self._pc_source()
        pc_ids = {doc.id for doc in pc_documents}
        private_error = await self._refresh_private(skip=pc_ids)
        stored = await self._backend.stored()
        desired = {doc.id: doc for doc in pc_documents}
        desired.update(self._private_documents)
        keep = set(desired)
        if private_error:
            kept = {i for i, row in stored.items() if row.source == SOURCE_PRIVATE_STAC}
            keep |= kept
            self.private_ids |= kept
        report, rows = await self._diff(desired, stored, dimension)
        if rows:
            await self._backend.upsert(rows)
        if keep:
            report.removed = await self._backend.delete_missing(keep)
        self.count = len(keep)
        self.last_error = "; ".join(e for e in (pc_reason, private_error) if e) or None
        logger.info(f"[VS] index built: {report}")
        return report

    async def _diff(
        self,
        desired: Dict[str, CollectionDocument],
        stored: Dict[str, StoredDocument],
        dimension: int,
    ) -> Tuple[BuildReport, List[StoredDocument]]:
        """Return the build report and the rows to write."""
        model = self._embedder.model_name if self._embedder else ""
        report = BuildReport()
        rows: List[StoredDocument] = []
        to_embed: List[CollectionDocument] = []
        for doc_id, doc in desired.items():
            old = stored.get(doc_id)
            if old is None or not _same_vector(old, doc, model, dimension):
                to_embed.append(doc)
                report.added += old is None
                report.updated += old is not None
                continue
            row = _stored_row(doc, old.embedding, model)
            if _same_metadata(old, row):
                report.unchanged += 1
            else:
                report.updated += 1
                rows.append(row)
        if to_embed:
            vectors = await self._embedder.embed_documents([d.text for d in to_embed])
            rows += [_stored_row(d, v, model) for d, v in zip(to_embed, vectors)]
            report.embedded = len(to_embed)
        return report, rows

    async def _refresh_private(self, skip: Set[str]) -> Optional[str]:
        """Read the private STAC catalog; on failure keep the previous one.

        Returns the failure reason, or None. Private collections kept from a
        previous run stay searchable but are not renderable until the private
        STAC API answers again, since renderability is not stored.
        """
        if not self._stac_api_url:
            self._private_documents = {}
            self.private_renderable, self.private_ids = set(), set()
            return None
        try:
            catalog = await self._private_source(self._stac_api_url, skip)
        except sources.SourceUnavailableError as exc:
            return str(exc)
        self._private_documents = {doc.id: doc for doc in catalog.documents}
        self.private_ids = set(self._private_documents)
        self.private_renderable = set(catalog.renderable_ids)
        return None

    async def check(self) -> None:
        """Re-check the backend; raises when it cannot serve searches."""
        self._ensure_components()
        await self._backend.check(await self._dimension())

    async def tick(self) -> None:
        """One background step: retry, check, or refresh as due."""
        if self.status == STATUS_DISABLED or self.settings.error:
            return
        if not self.built:
            await self._attempt(self.build)
        elif time.monotonic() - self.last_build_at >= REFRESH_INTERVAL_SECONDS:
            await self._refresh()
        else:
            await self._attempt(self.check)

    async def _refresh(self) -> None:
        """Rebuild; a failure keeps the last good index and records the error."""
        try:
            await self.build()
        except BackendNotReadyError as exc:
            self._set_status(STATUS_BUILDING, str(exc))
        except Exception as exc:
            self.last_error = f"Refresh failed: {_reason(exc)}"
            self.last_build_at = time.monotonic()
            await self._attempt(self.check)
        else:
            self._set_status(STATUS_READY)

    async def run(self) -> None:
        """Start, then check every 5 minutes and refresh every 24 hours."""
        await self.start()
        while self.status != STATUS_DISABLED and not self.settings.error:
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)
            await self.tick()

    async def search(self, query: SearchQuery) -> List[BackendHit]:
        """Embed the query when needed and search, bounded by the timeout."""
        if not self.ready:
            raise BackendUnavailableError(self.unavailable_message())
        self._ensure_components()
        return await asyncio.wait_for(
            self._search(query), timeout=self.settings.timeout_seconds
        )

    async def _search(self, query: SearchQuery) -> List[BackendHit]:
        """Unbounded search body."""
        if self._embedder is not None:
            vector = await self._embedder.embed_query(query.text)
            query = SearchQuery(**{**query.__dict__, "vector": vector})
        return await self._backend.search(query)

    def unavailable_message(self) -> str:
        """Return why searches are not served right now."""
        if self.status == STATUS_DISABLED:
            return "Vector search is disabled (VECTOR_SEARCH_ENABLED=false)."
        if self.status == STATUS_BUILDING:
            return "The vector search index is still building."
        return f"Vector search is degraded: {self.reason or 'unknown reason'}"


def _reason(exc: BaseException) -> str:
    """Return a readable reason for *exc*."""
    return str(exc) or exc.__class__.__name__


def _same_vector(
    old: StoredDocument, doc: CollectionDocument, model: str, dimension: int
) -> bool:
    """Return True when the stored vector can be reused for *doc*."""
    return (
        old.content_hash == doc.content_hash
        and old.model == model
        and (not dimension or old.dimension == dimension)
    )


def _same_metadata(old: StoredDocument, new: StoredDocument) -> bool:
    """Return True when nothing but the vector would change."""
    keys = ("title", "description", "bbox", "start", "end", "source")
    return all(getattr(old, key) == getattr(new, key) for key in keys)


def _stored_row(
    doc: CollectionDocument, embedding: List[float], model: str
) -> StoredDocument:
    """Return the stored row for *doc* with *embedding*."""
    return StoredDocument(
        id=doc.id,
        title=doc.title,
        description=doc.description,
        embedding=list(embedding),
        bbox=doc.bbox,
        start=doc.start,
        end=doc.end,
        model=model,
        content_hash=doc.content_hash,
        source=doc.source,
    )


_index: Optional[VectorIndex] = None


def get_index() -> VectorIndex:
    """Return the process-wide index, created from the environment once."""
    global _index
    if _index is None:
        _index = VectorIndex(VectorSearchSettings.from_env())
    return _index


def reset_index() -> None:
    """Drop the process-wide index (used by tests)."""
    global _index
    _index = None
