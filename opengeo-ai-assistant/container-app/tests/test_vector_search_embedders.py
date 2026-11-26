"""Embedders: provider selection, credentials, batching and the query LRU."""

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List

import llm_client
import numpy as np
import pytest
from services.vector_search import embedders
from services.vector_search.settings import VectorSearchSettings

OPENAI_ENV = {
    "EMBEDDING_PROVIDER": "openai",
    "EMBEDDING_MODEL": "text-embedding-3-small",
}


class FakeEmbeddingsApi:
    """Stands in for AsyncOpenAI.embeddings and records every request."""

    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []

    async def create(self, model: str, input: List[str]) -> SimpleNamespace:
        self.requests.append({"model": model, "input": list(input)})
        data = [
            SimpleNamespace(index=i, embedding=[float(len(text)), 1.0])
            for i, text in reversed(list(enumerate(input)))
        ]
        return SimpleNamespace(data=data)


@pytest.fixture
def openai_calls(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    """Capture the arguments the OpenAI SDK client is built with."""
    calls: List[Dict[str, Any]] = []
    api = FakeEmbeddingsApi()

    def fake_client(**kwargs: Any) -> SimpleNamespace:
        calls.append({**kwargs, "api": api})
        return SimpleNamespace(embeddings=api)

    monkeypatch.setattr(llm_client, "AsyncOpenAI", fake_client)
    return calls


def build(**env: str) -> Any:
    return embedders.create_embedder(VectorSearchSettings.from_env(env))


def test_openai_with_openai_llm_uses_fallback_credentials(openai_calls) -> None:
    embedder = build(
        LLM_PROVIDER="openai",
        LLM_API_KEY="llm-key",
        LLM_BASE_URL="http://localhost:11434/v1",
        **OPENAI_ENV,
    )
    vectors = asyncio.run(embedder.embed_documents(["ab", "abcd"]))
    call = openai_calls[0]
    assert (call["api_key"], call.get("base_url")) == (
        "llm-key",
        "http://localhost:11434/v1",
    )
    assert call["api"].requests == [
        {"model": "text-embedding-3-small", "input": ["ab", "abcd"]}
    ]
    assert vectors == [[2.0, 1.0], [4.0, 1.0]]


def test_openai_with_anthropic_llm_never_sends_llm_key(openai_calls) -> None:
    embedder = build(
        LLM_PROVIDER="anthropic",
        LLM_API_KEY="sk-ant-secret",
        EMBEDDING_API_KEY="emb-key",
        **OPENAI_ENV,
    )
    asyncio.run(embedder.embed_query("fires"))
    assert openai_calls[0]["api_key"] == "emb-key"
    assert "base_url" not in openai_calls[0]


def test_openai_client_is_bounded_by_the_timeout(openai_calls) -> None:
    embedder = build(
        LLM_PROVIDER="openai",
        LLM_API_KEY="k",
        VECTOR_SEARCH_TIMEOUT_SECONDS="2.5",
        **OPENAI_ENV,
    )
    asyncio.run(embedder.embed_query("x"))
    assert (openai_calls[0]["timeout"], openai_calls[0]["max_retries"]) == (2.5, 0)


def test_openai_batches_and_measures_dimension(openai_calls, monkeypatch) -> None:
    monkeypatch.setattr(embedders, "REMOTE_BATCH_SIZE", 2)
    embedder = build(LLM_PROVIDER="openai", LLM_API_KEY="k", **OPENAI_ENV)
    asyncio.run(embedder.embed_documents(["a", "b", "c"]))
    assert asyncio.run(embedder.dimension()) == 2
    sizes = [len(r["input"]) for r in openai_calls[0]["api"].requests]
    assert sizes == [2, 1, 1]


def test_openai_repeated_query_served_from_lru(openai_calls) -> None:
    embedder = build(LLM_PROVIDER="openai", LLM_API_KEY="k", **OPENAI_ENV)

    async def twice() -> None:
        await embedder.embed_query("burn severity")
        await embedder.embed_query("burn severity")

    asyncio.run(twice())
    assert len(openai_calls[0]["api"].requests) == 1


class FakeTextEmbedding:
    """Stands in for fastembed.TextEmbedding and counts model calls."""

    instances: List["FakeTextEmbedding"] = []

    def __init__(self, model_name: str, **kwargs: Any) -> None:
        self.model_name = model_name
        self.kwargs = kwargs
        self.query_calls = 0
        FakeTextEmbedding.instances.append(self)

    def embed(self, texts: List[str], batch_size: int = 32):
        return (np.array([float(len(t)), 0.0, 1.0]) for t in texts)

    def query_embed(self, texts: List[str]):
        self.query_calls += 1
        return (np.array([float(len(t)), 1.0, 0.0]) for t in texts)


@pytest.fixture
def fake_fastembed(monkeypatch: pytest.MonkeyPatch) -> type:
    import fastembed

    FakeTextEmbedding.instances = []
    monkeypatch.setattr(fastembed, "TextEmbedding", FakeTextEmbedding)
    return FakeTextEmbedding


def test_local_default_model_loads_offline_only(fake_fastembed) -> None:
    embedder = build()
    assert isinstance(embedder, embedders.LocalEmbedder)
    assert asyncio.run(embedder.embed_documents(["abc"])) == [[3.0, 0.0, 1.0]]
    model = fake_fastembed.instances[0]
    assert model.kwargs == {"cache_dir": "/opt/models", "local_files_only": True}


def test_local_other_model_may_download(fake_fastembed) -> None:
    embedder = build(EMBEDDING_MODEL="BAAI/bge-small-en-v1.5")
    asyncio.run(embedder.embed_query("x"))
    assert fake_fastembed.instances[0].kwargs["local_files_only"] is False


def test_local_repeated_query_served_from_lru(fake_fastembed) -> None:
    embedder = build()

    async def run() -> int:
        await embedder.embed_query("feux de forêt")
        await embedder.embed_query("feux de forêt")
        return await embedder.dimension()

    assert asyncio.run(run()) == 3
    assert fake_fastembed.instances[0].query_calls == 2  # query + dimension probe


def test_model_loaded_once_under_concurrency(fake_fastembed) -> None:
    embedder = build()

    async def run() -> None:
        await asyncio.gather(*(embedder.embed_query(f"q{i}") for i in range(5)))

    asyncio.run(run())
    assert len(fake_fastembed.instances) == 1


@pytest.mark.parametrize(
    "env",
    [
        {"VECTOR_SEARCH_URL": "https://search.example.com/v1/search"},
        {"VECTOR_SEARCH_ENABLED": "false"},
    ],
)
def test_no_embedder_when_not_needed(env: Dict[str, str], fake_fastembed) -> None:
    assert build(**env) is None
    assert fake_fastembed.instances == []
