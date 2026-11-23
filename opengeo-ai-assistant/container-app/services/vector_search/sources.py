"""Collection sources: renderable Planetary Computer collections and private STAC."""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote, urljoin, urlsplit

import aiohttp
from pc_tasks_config_loader import (
    get_collection_description,
    get_collection_keywords,
    get_collection_title,
)
from services import dashboard_catalog

from .documents import (
    SOURCE_PLANETARY_COMPUTER,
    SOURCE_PRIVATE_STAC,
    Bbox,
    CollectionDocument,
    normalize_bbox,
    parse_instant,
)
from .settings import mask_url

logger = logging.getLogger(__name__)

DESCRIPTIONS_FILE = Path(__file__).with_name("collection_descriptions.json")
PROBE_CONCURRENCY = 8
MAX_COLLECTION_PAGES = 20
STAC_TIMEOUT_SECONDS = 15


class SourceUnavailableError(RuntimeError):
    """A collection source could not be read; str() is the reason."""


@dataclass
class PrivateCatalog:
    """Private STAC documents and the ids among them the map can render."""

    documents: List[CollectionDocument] = field(default_factory=list)
    renderable_ids: Set[str] = field(default_factory=set)


def stac_base_url(stac_api_url: str) -> str:
    """Return the STAC root from STAC_API_URL (a root or a /search URL)."""
    return stac_api_url.strip().rsplit("/search", 1)[0].rstrip("/")


def _interval(values: Optional[List[Optional[str]]]) -> Tuple[Any, Any]:
    """Parse a STAC [start, end] interval; unreadable ends become open."""
    if not values or len(values) != 2:
        return None, None
    return _instant(values[0]), _instant(values[1])


def _instant(value: Optional[str]) -> Optional[datetime]:
    """Parse one STAC datetime, or None when missing or malformed."""
    if not value:
        return None
    try:
        return parse_instant(value)
    except ValueError:
        return None


async def planetary_computer_documents() -> Tuple[List[CollectionDocument], str]:
    """Return every renderable collection, and a reason when extents are missing.

    Text comes from the local rendering configuration (no network). Extents come
    from the dashboard catalog when it can be fetched; otherwise the documents are
    global and open-ended until a later refresh.
    """
    reason = ""
    bboxes: Dict[str, List[float]] = {}
    try:
        catalog = await dashboard_catalog.get_catalog()
        bboxes = {entry["id"]: entry["bbox"] for entry in catalog["collections"]}
    except Exception as exc:
        reason = f"Planetary Computer extents unavailable: {exc}"
    intervals = dashboard_catalog.collection_intervals()
    documents = []
    for config_id, stac_id in sorted(
        dashboard_catalog.renderable_collections().items(), key=lambda item: item[1]
    ):
        start, end = _interval(intervals.get(stac_id))
        text = _search_text(config_id, stac_id)
        documents.append(
            CollectionDocument(
                id=stac_id,
                title=text["title"],
                description=text["description"],
                keywords=tuple(text["keywords"]),
                bbox=normalize_bbox(bboxes.get(stac_id)),
                start=start,
                end=end,
                source=SOURCE_PLANETARY_COMPUTER,
            )
        )
    return documents, reason


@lru_cache(maxsize=1)
def _description_overrides() -> Dict[str, Dict[str, Any]]:
    """Load vetted search text for collections with placeholder descriptions."""
    data = json.loads(DESCRIPTIONS_FILE.read_text(encoding="utf-8"))
    return {key: value for key, value in data.items() if not key.startswith("_")}


def _search_text(config_id: str, stac_id: str) -> Dict[str, Any]:
    """Return title, description and keywords, preferring the vetted overrides."""
    override = _description_overrides().get(stac_id)
    if override:
        return override
    return {
        "title": get_collection_title(config_id) or stac_id,
        "description": get_collection_description(config_id) or "",
        "keywords": get_collection_keywords(config_id) or [],
    }


def private_document(collection: Dict[str, Any]) -> CollectionDocument:
    """Build a document from one STAC collection JSON."""
    extent = collection.get("extent") or {}
    bboxes = (extent.get("spatial") or {}).get("bbox") or []
    intervals = (extent.get("temporal") or {}).get("interval") or []
    start, end = _interval(intervals[0] if intervals else None)
    bbox: Optional[Bbox] = normalize_bbox(bboxes[0]) if bboxes else None
    return CollectionDocument(
        id=collection["id"],
        title=collection.get("title") or collection["id"],
        description=collection.get("description") or "",
        keywords=tuple(str(k) for k in collection.get("keywords") or ()),
        bbox=bbox,
        start=start,
        end=end,
        source=SOURCE_PRIVATE_STAC,
    )


async def _get_json(session: aiohttp.ClientSession, url: str) -> Dict[str, Any]:
    """GET *url* without following redirects; non-2xx statuses raise."""
    async with session.get(url, allow_redirects=False) as response:
        if response.status // 100 != 2:
            raise aiohttp.ClientResponseError(
                response.request_info, response.history, status=response.status
            )
        return await response.json(content_type=None)


def same_origin_url(base: str, href: Optional[str]) -> Optional[str]:
    """Resolve *href* against *base*; return it only on the same scheme and host."""
    if not href:
        return None
    resolved = urljoin(f"{base}/", href)
    origin, target = urlsplit(base), urlsplit(resolved)
    if target.scheme not in ("http", "https"):
        return None
    if (target.scheme, target.netloc) != (origin.scheme, origin.netloc):
        logger.info(f"[VS] Ignoring off-host STAC link {mask_url(resolved)}")
        return None
    return resolved


async def _list_collections(
    session: aiohttp.ClientSession, base: str
) -> List[Dict[str, Any]]:
    """Return every collection of the STAC API, following same-host `next` links."""
    url: Optional[str] = f"{base}/collections"
    collections: List[Dict[str, Any]] = []
    for _ in range(MAX_COLLECTION_PAGES):
        if not url:
            break
        page = await _get_json(session, url)
        collections += [c for c in page.get("collections", []) if c.get("id")]
        href = next(
            (
                link.get("href")
                for link in page.get("links", [])
                if link.get("rel") == "next"
            ),
            None,
        )
        url = same_origin_url(base, href)
    return collections


async def _has_tilejson(
    session: aiohttp.ClientSession,
    base: str,
    collection_id: str,
    gate: asyncio.Semaphore,
) -> bool:
    """Return True when the collection's first item has a tilejson asset."""
    async with gate:
        try:
            page = await _get_json(
                session,
                f"{base}/collections/{quote(collection_id, safe='')}/items?limit=1",
            )
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            logger.info(f"[VS] No sample item for {collection_id}: {exc}")
            return False
    features = page.get("features") or []
    assets = (features[0].get("assets") or {}) if features else {}
    return bool((assets.get("tilejson") or {}).get("href"))


async def private_stac_catalog(stac_api_url: str, skip_ids: Set[str]) -> PrivateCatalog:
    """Read the private STAC API's collections and probe which are renderable.

    Collections whose id is in *skip_ids* (Planetary Computer ids) are left out.
    Raises SourceUnavailableError when the collection list cannot be read.
    """
    base = stac_base_url(stac_api_url)
    timeout = aiohttp.ClientTimeout(total=STAC_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            collections = await _list_collections(session, base)
            documents = [
                private_document(c) for c in collections if c["id"] not in skip_ids
            ]
            gate = asyncio.Semaphore(PROBE_CONCURRENCY)
            flags = await asyncio.gather(
                *(_has_tilejson(session, base, d.id, gate) for d in documents)
            )
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        raise SourceUnavailableError(
            f"Private STAC API {mask_url(base)} is unavailable: {type(exc).__name__}"
        ) from exc
    renderable = {doc.id for doc, flag in zip(documents, flags) if flag}
    return PrivateCatalog(documents=documents, renderable_ids=renderable)
