"""Vector search settings: backend selection, modes, providers and masking."""

import re
from pathlib import Path
from typing import Dict, Optional

import pytest
from services.vector_search import settings as vs

ENV_EXAMPLE = next(
    (
        parent / ".env.example"
        for parent in Path(__file__).resolve().parents
        if (parent / ".env.example").exists()
    ),
    Path("/nonexistent/.env.example"),
)
OPENAI_LLM = {"LLM_PROVIDER": "openai", "LLM_API_KEY": "llm-key"}


def resolve(**env: str) -> vs.VectorSearchSettings:
    """Resolve settings from exactly *env*, ignoring the process environment."""
    return vs.VectorSearchSettings.from_env(env)


@pytest.mark.parametrize(
    "url, backend, remote",
    [
        ("", vs.BACKEND_PARQUET, False),
        ("file:///data/index.parquet", vs.BACKEND_PARQUET, False),
        ("s3://bucket/index.parquet", vs.BACKEND_PARQUET, True),
        ("https://host/data/index.parquet", vs.BACKEND_PARQUET, True),
        ("postgresql://user:pw@db:5432/geo", vs.BACKEND_POSTGRES, False),
        ("mongodb://db:27017/geo", vs.BACKEND_MONGODB, False),
        ("mongodb+srv://cluster.example.net/geo", vs.BACKEND_MONGODB, False),
        ("https://search.example.com/v1/search", vs.BACKEND_HTTP, False),
    ],
)
def test_scheme_selects_backend(url: str, backend: str, remote: bool) -> None:
    settings = resolve(VECTOR_SEARCH_URL=url)
    assert (settings.backend, settings.remote, settings.error) == (
        backend,
        remote,
        None,
    )


def test_empty_url_is_the_managed_built_in_file() -> None:
    settings = resolve()
    assert settings.location == vs.DEFAULT_INDEX_PATH
    assert settings.mode == vs.MODE_MANAGED
    assert settings.enabled


def test_file_url_points_at_its_path() -> None:
    assert resolve(VECTOR_SEARCH_URL="file:///data/x.parquet").location == (
        "/data/x.parquet"
    )


def test_parquet_suffix_beats_http_retriever() -> None:
    settings = resolve(VECTOR_SEARCH_URL="http://localhost:9000/INDEX.PARQUET")
    assert settings.backend == vs.BACKEND_PARQUET


@pytest.mark.parametrize(
    "url",
    ["https://search.example.com/v1/search", "s3://b/i.parquet", "https://h/i.parquet"],
)
def test_read_only_backends_are_always_external(url: str) -> None:
    settings = resolve(VECTOR_SEARCH_URL=url, VECTOR_SEARCH_MODE="managed")
    assert settings.mode == vs.MODE_EXTERNAL


def test_external_mode_is_kept_for_databases() -> None:
    settings = resolve(
        VECTOR_SEARCH_URL="postgresql://db/geo", VECTOR_SEARCH_MODE="EXTERNAL"
    )
    assert settings.mode == vs.MODE_EXTERNAL


@pytest.mark.parametrize(
    "url", ["ftp://host/x", "redis://cache:6379", "/data/x.parquet"]
)
def test_unknown_scheme_names_supported_schemes(url: str) -> None:
    error = resolve(VECTOR_SEARCH_URL=url).error
    assert error and "postgresql://" in error and "mongodb+srv://" in error


def test_unknown_mode_is_a_reason() -> None:
    assert "VECTOR_SEARCH_MODE" in (resolve(VECTOR_SEARCH_MODE="hybrid").error or "")


@pytest.mark.parametrize("value", ["false", "FALSE", "0", "off"])
def test_disabled_values(value: str) -> None:
    assert not resolve(VECTOR_SEARCH_ENABLED=value).enabled


def test_mongo_database_from_url_path() -> None:
    assert resolve(VECTOR_SEARCH_URL="mongodb://db/geo").mongo_database == "geo"
    srv = resolve(VECTOR_SEARCH_URL="mongodb+srv://u:p@cluster.net/?retryWrites=true")
    assert srv.mongo_database == vs.DEFAULT_MONGO_DATABASE


def test_none_provider_requires_http_retriever() -> None:
    error = resolve(EMBEDDING_PROVIDER="none", VECTOR_SEARCH_URL="postgresql://db/geo")
    assert "requires the HTTP retriever" in (error.error or "")
    http = resolve(EMBEDDING_PROVIDER="none", VECTOR_SEARCH_URL="https://h/search")
    assert http.error is None


def test_http_retriever_needs_no_embedder() -> None:
    settings = resolve(VECTOR_SEARCH_URL="https://h/search")
    assert not settings.uses_embedder
    assert resolve().uses_embedder


def test_openai_requires_model() -> None:
    settings = resolve(EMBEDDING_PROVIDER="openai", **OPENAI_LLM)
    assert "EMBEDDING_MODEL is required" in (settings.error or "")


def test_openai_reuses_llm_credentials_for_openai_llm() -> None:
    settings = resolve(
        EMBEDDING_PROVIDER="openai",
        EMBEDDING_MODEL="text-embedding-3-small",
        LLM_BASE_URL="http://localhost:11434/v1",
        **OPENAI_LLM,
    )
    assert settings.error is None
    assert settings.embedding_api_key == "llm-key"
    assert settings.embedding_base_url == "http://localhost:11434/v1"


def test_anthropic_llm_key_is_never_the_embedding_key() -> None:
    settings = resolve(
        EMBEDDING_PROVIDER="openai",
        EMBEDDING_MODEL="text-embedding-3-small",
        LLM_PROVIDER="anthropic",
        LLM_API_KEY="sk-ant-secret",
        LLM_BASE_URL="https://api.anthropic.com/v1",
    )
    assert settings.embedding_api_key is None
    assert settings.embedding_base_url is None
    assert "EMBEDDING_API_KEY (or LLM_API_KEY" in (settings.error or "")


def test_explicit_embedding_credentials_win() -> None:
    settings = resolve(
        EMBEDDING_PROVIDER="openai",
        EMBEDDING_MODEL="m",
        EMBEDDING_API_KEY="emb-key",
        **OPENAI_LLM,
    )
    assert settings.embedding_api_key == "emb-key"


def test_unsafe_embedding_base_url_is_a_reason_not_an_exception() -> None:
    settings = resolve(
        EMBEDDING_PROVIDER="openai",
        EMBEDDING_MODEL="m",
        EMBEDDING_BASE_URL="http://gpu-box:8000/v1",
        **OPENAI_LLM,
    )
    assert "EMBEDDING_BASE_URL uses http:// with non-local host" in (
        settings.error or ""
    )


def test_unsafe_table_name_is_a_reason() -> None:
    settings = resolve(
        VECTOR_SEARCH_URL="postgresql://db/geo", VECTOR_SEARCH_TABLE="x; DROP TABLE y"
    )
    assert "VECTOR_SEARCH_TABLE" in (settings.error or "")


@pytest.mark.parametrize("value", ["abc", "0", "-1"])
def test_invalid_timeout_is_a_reason(value: str) -> None:
    settings = resolve(VECTOR_SEARCH_TIMEOUT_SECONDS=value)
    assert settings.timeout_seconds == vs.DEFAULT_TIMEOUT_SECONDS
    assert "VECTOR_SEARCH_TIMEOUT_SECONDS" in (settings.error or "")


def test_s3_variables_are_collected() -> None:
    settings = resolve(AWS_ACCESS_KEY_ID="id", AWS_REGION="ca-central-1")
    assert settings.s3 == {"AWS_ACCESS_KEY_ID": "id", "AWS_REGION": "ca-central-1"}


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://user:secret@db:5432/geo",
        "mongodb+srv://user:secret@cluster.net/geo?authSource=secret",
        "https://search.example.com/v1/search?token=secret",
    ],
)
def test_masking_hides_secrets(url: str) -> None:
    settings = resolve(VECTOR_SEARCH_URL=url, VECTOR_SEARCH_API_KEY="secret")
    assert "secret" not in settings.display_url
    assert "secret" not in repr(settings)


@pytest.mark.parametrize(
    "url", ["https://secret@search.example.com/v1", "mongodb://secret@db/geo"]
)
def test_masking_hides_credential_usernames(url: str) -> None:
    assert "secret" not in vs.mask_url(url)


def test_masking_keeps_host() -> None:
    assert vs.mask_url("postgresql://user:pw@db:5432/geo") == (
        "postgresql://user:***@db:5432/geo"
    )


def _env_example_keys(text: str) -> set:
    """Return every VECTOR_SEARCH_* and EMBEDDING_* key named in *text*."""
    return set(re.findall(r"^#?\s*((?:VECTOR_SEARCH|EMBEDDING)_[A-Z_]+)=", text, re.M))


def test_env_example_keys_are_recognised() -> None:
    if not ENV_EXAMPLE.exists():
        pytest.skip(".env.example is not available (tests run inside the image)")
    keys = _env_example_keys(ENV_EXAMPLE.read_text())
    assert keys, "the .env.example vector search section is missing"
    assert keys <= vs.KNOWN_ENV_KEYS


def _no_error(settings: vs.VectorSearchSettings) -> Optional[str]:
    return settings.error


def test_defaults_have_no_error() -> None:
    env: Dict[str, str] = {}
    assert _no_error(vs.VectorSearchSettings.from_env(env)) is None
