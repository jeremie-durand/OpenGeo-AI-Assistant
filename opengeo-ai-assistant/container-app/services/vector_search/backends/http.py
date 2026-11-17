"""HTTP retriever backend: the integrator's own search API ranks the results."""

import asyncio
from numbers import Real
from typing import Any, Collection, Dict, List, Optional, Sequence

import aiohttp

from .base import (
    BackendConfigError,
    BackendHit,
    BackendInfo,
    BackendUnavailableError,
    SearchQuery,
    StoredDocument,
)

HEALTH_CHECK_QUERY = "vector search health check"


def safe_error(exc: BaseException) -> str:
    """Describe *exc* without its request details (headers hold the token)."""
    status = getattr(exc, "status", None)
    name = type(exc).__name__
    return f"{name} (HTTP {status})" if isinstance(status, int) else name


def request_body(query: SearchQuery) -> Dict[str, Any]:
    """Return the JSON body of the HTTP retriever contract."""
    body: Dict[str, Any] = {"query": query.text, "k": query.k, "kind": query.kind}
    if query.bbox is not None:
        body["bbox"] = list(query.bbox)
    if query.datetime_text:
        body["datetime"] = query.datetime_text
    return body


def parse_results(payload: Any) -> List[BackendHit]:
    """Validate a `{"results": [{"id", "score", ...}]}` response."""
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        raise BackendUnavailableError("The HTTP retriever returned no results list.")
    hits = []
    for item in results:
        if not isinstance(item, dict):
            raise BackendUnavailableError("The HTTP retriever returned a bad result.")
        doc_id, score = item.get("id"), item.get("score")
        if not isinstance(doc_id, str) or not doc_id:
            raise BackendUnavailableError("An HTTP retriever result has no id.")
        if isinstance(score, bool) or not isinstance(score, Real):
            raise BackendUnavailableError(f"Result '{doc_id}' has no numeric score.")
        hits.append(
            BackendHit(
                id=doc_id,
                score=float(score),
                title=str(item.get("title") or ""),
                description=str(item.get("description") or ""),
            )
        )
    return hits


class HttpRetrieverBackend:
    """POSTs the query text to the integrator's URL; always read-only."""

    read_only = True

    def __init__(self, url: str, api_key: Optional[str], timeout: float) -> None:
        self._url = url
        self._api_key = api_key
        self._timeout = timeout

    def _headers(self) -> Dict[str, str]:
        """Return request headers, with the bearer token when configured."""
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def search(self, query: SearchQuery) -> List[BackendHit]:
        """Send the query; any failure counts as unavailable, never as empty."""
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    self._url, json=request_body(query), headers=self._headers()
                ) as response:
                    if response.status // 100 != 2:
                        raise BackendUnavailableError(
                            f"The HTTP retriever answered HTTP {response.status}."
                        )
                    payload = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise BackendUnavailableError(
                f"The HTTP retriever failed: {safe_error(exc)}"
            ) from None
        return parse_results(payload)[: query.k]

    async def stored(self) -> Dict[str, StoredDocument]:
        """The HTTP retriever stores nothing the assistant can read."""
        raise BackendConfigError("The HTTP retriever is read-only.")

    async def upsert(self, documents: Sequence[StoredDocument]) -> None:
        """The HTTP retriever cannot be written."""
        raise BackendConfigError("The HTTP retriever is read-only.")

    async def delete_missing(self, keep_ids: Collection[str]) -> int:
        """The HTTP retriever cannot be written."""
        raise BackendConfigError("The HTTP retriever is read-only.")

    async def describe(self) -> BackendInfo:
        """Nothing is known about the integrator's store."""
        return BackendInfo()

    async def check(self, dimension: Optional[int]) -> None:
        """Probe the retriever with a one-result search."""
        await self.search(SearchQuery(text=HEALTH_CHECK_QUERY, k=1))
