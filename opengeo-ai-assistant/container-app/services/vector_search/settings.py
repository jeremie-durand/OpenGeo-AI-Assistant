"""Vector search configuration read from the environment."""

import os
import re
from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DEFAULT_INDEX_PATH = "/app/data/vector-index.parquet"
DEFAULT_MODEL_DIR = "/opt/models"
DEFAULT_EXTENSION_DIR = "/opt/duckdb_extensions"
DEFAULT_TABLE = "opengeo.collection_embeddings"
DEFAULT_MONGO_COLLECTION = "opengeo_search_collections"
DEFAULT_MONGO_INDEX = "opengeo_vector_index"
DEFAULT_MONGO_DATABASE = "opengeo"
DEFAULT_TIMEOUT_SECONDS = 5.0

BACKEND_PARQUET = "parquet"
BACKEND_POSTGRES = "postgres"
BACKEND_MONGODB = "mongodb"
BACKEND_HTTP = "http"

MODE_MANAGED = "managed"
MODE_EXTERNAL = "external"
MODES = (MODE_MANAGED, MODE_EXTERNAL)

PROVIDER_LOCAL = "local"
PROVIDER_OPENAI = "openai"
PROVIDER_NONE = "none"
PROVIDERS = (PROVIDER_LOCAL, PROVIDER_OPENAI, PROVIDER_NONE)

SUPPORTED_SCHEMES = (
    "file://, s3://, http(s)://...parquet, postgresql://, mongodb://, "
    "mongodb+srv://, http(s)://"
)
AWS_ENV_KEYS = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_REGION",
    "AWS_ENDPOINT_URL",
)
KNOWN_ENV_KEYS = frozenset(
    {
        "VECTOR_SEARCH_ENABLED",
        "VECTOR_SEARCH_URL",
        "VECTOR_SEARCH_MODE",
        "VECTOR_SEARCH_TIMEOUT_SECONDS",
        "VECTOR_SEARCH_TABLE",
        "VECTOR_SEARCH_COLLECTION",
        "VECTOR_SEARCH_MONGO_INDEX",
        "VECTOR_SEARCH_API_KEY",
        "VECTOR_SEARCH_MODEL_DIR",
        "EMBEDDING_PROVIDER",
        "EMBEDDING_MODEL",
        "EMBEDDING_BASE_URL",
        "EMBEDDING_API_KEY",
        *AWS_ENV_KEYS,
    }
)
FALSE_VALUES = {"false", "0", "no", "off"}
IDENTIFIER_RE = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)?$")
MASK = "***"


@dataclass(frozen=True)
class VectorSearchSettings:
    """Resolved vector search settings; `error` holds the degraded reason, if any."""

    enabled: bool = True
    url: str = field(default="", repr=False)
    backend: str = BACKEND_PARQUET
    location: str = field(default=DEFAULT_INDEX_PATH, repr=False)
    remote: bool = False
    mode: str = MODE_MANAGED
    provider: str = PROVIDER_LOCAL
    model: str = DEFAULT_MODEL
    model_dir: str = DEFAULT_MODEL_DIR
    extension_dir: str = DEFAULT_EXTENSION_DIR
    embedding_base_url: Optional[str] = field(default=None, repr=False)
    embedding_api_key: Optional[str] = field(default=None, repr=False)
    table: str = DEFAULT_TABLE
    mongo_collection: str = DEFAULT_MONGO_COLLECTION
    mongo_index: str = DEFAULT_MONGO_INDEX
    mongo_database: str = DEFAULT_MONGO_DATABASE
    api_key: Optional[str] = field(default=None, repr=False)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    s3: Dict[str, str] = field(default_factory=dict, repr=False)
    error: Optional[str] = None

    @property
    def uses_embedder(self) -> bool:
        """Return True when searches need a local or remote embedding model."""
        return self.enabled and self.backend != BACKEND_HTTP

    @property
    def display_url(self) -> str:
        """Return the configured URL with credentials masked."""
        return mask_url(self.url) if self.url else self.location

    @classmethod
    def from_env(
        cls, env: Optional[Mapping[str, str]] = None
    ) -> "VectorSearchSettings":
        """Build settings from *env* (defaults to os.environ); never raises."""
        source = os.environ if env is None else env
        return _resolve(source)


def mask_url(url: str) -> str:
    """Hide the password and every query value of *url*."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return MASK
    netloc = parts.netloc
    if "@" in netloc:
        host = netloc.rsplit("@", 1)[1]
        user = parts.username if parts.password is not None else MASK
        password = f":{MASK}" if parts.password is not None else ""
        netloc = f"{user}{password}@{host}"
    query = "&".join(
        f"{pair.split('=', 1)[0]}={MASK}" for pair in parts.query.split("&") if pair
    )
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def _get(env: Mapping[str, str], key: str, default: str = "") -> str:
    """Return a stripped environment value, or *default* when unset or blank."""
    return (env.get(key) or "").strip() or default


def _resolve(env: Mapping[str, str]) -> VectorSearchSettings:
    """Resolve every setting, recording the first configuration error found."""
    url = _get(env, "VECTOR_SEARCH_URL")
    backend, location, remote, scheme_error = _parse_url(url)
    mode, mode_error = _parse_mode(_get(env, "VECTOR_SEARCH_MODE", MODE_MANAGED))
    if backend == BACKEND_HTTP or remote:
        mode = MODE_EXTERNAL
    provider = _get(env, "EMBEDDING_PROVIDER", PROVIDER_LOCAL).lower()
    base_url, api_key = _embedding_credentials(env)
    timeout, timeout_error = _parse_timeout(env)
    settings = VectorSearchSettings(
        enabled=_get(env, "VECTOR_SEARCH_ENABLED", "true").lower() not in FALSE_VALUES,
        url=url,
        backend=backend,
        location=location,
        remote=remote,
        mode=mode,
        provider=provider,
        model=_get(env, "EMBEDDING_MODEL", _default_model(provider)),
        model_dir=_get(env, "VECTOR_SEARCH_MODEL_DIR", DEFAULT_MODEL_DIR),
        extension_dir=_get(env, "DUCKDB_EXTENSION_DIR", DEFAULT_EXTENSION_DIR),
        embedding_base_url=base_url,
        embedding_api_key=api_key,
        table=_get(env, "VECTOR_SEARCH_TABLE", DEFAULT_TABLE),
        mongo_collection=_get(
            env, "VECTOR_SEARCH_COLLECTION", DEFAULT_MONGO_COLLECTION
        ),
        mongo_index=_get(env, "VECTOR_SEARCH_MONGO_INDEX", DEFAULT_MONGO_INDEX),
        mongo_database=_mongo_database(url, backend),
        api_key=_get(env, "VECTOR_SEARCH_API_KEY") or None,
        timeout_seconds=timeout,
        s3={key: env[key].strip() for key in AWS_ENV_KEYS if _get(env, key)},
    )
    error = scheme_error or mode_error or timeout_error or _combination_error(settings)
    return _with_error(settings, error)


def _with_error(
    settings: VectorSearchSettings, error: Optional[str]
) -> VectorSearchSettings:
    """Return *settings* with *error* recorded."""
    if not error:
        return settings
    values = {name: getattr(settings, name) for name in settings.__dataclass_fields__}
    values["error"] = error
    return VectorSearchSettings(**values)


def _parse_url(url: str) -> Tuple[str, str, bool, Optional[str]]:
    """Map *url* to (backend, location, remote, error) following its scheme."""
    if not url:
        return BACKEND_PARQUET, DEFAULT_INDEX_PATH, False, None
    try:
        parts = urlsplit(url)
    except ValueError:
        return BACKEND_PARQUET, "", False, _scheme_error("unparsable URL")
    scheme = parts.scheme.lower()
    if scheme == "file":
        return BACKEND_PARQUET, parts.path, False, None
    if scheme == "s3" or (
        scheme in ("http", "https") and parts.path.lower().endswith(".parquet")
    ):
        return BACKEND_PARQUET, url, True, None
    if scheme == "postgresql":
        return BACKEND_POSTGRES, url, False, None
    if scheme in ("mongodb", "mongodb+srv"):
        return BACKEND_MONGODB, url, False, None
    if scheme in ("http", "https"):
        return BACKEND_HTTP, url, False, None
    return BACKEND_PARQUET, "", False, _scheme_error(f"scheme '{scheme or '(none)'}'")


def _scheme_error(what: str) -> str:
    """Return the degraded reason for an unsupported VECTOR_SEARCH_URL."""
    return (
        f"VECTOR_SEARCH_URL has an unsupported {what}. "
        f"Supported schemes: {SUPPORTED_SCHEMES}."
    )


def _parse_mode(value: str) -> Tuple[str, Optional[str]]:
    """Return the requested mode, or managed plus a reason when it is unknown."""
    mode = value.lower()
    if mode in MODES:
        return mode, None
    return MODE_MANAGED, (
        f"VECTOR_SEARCH_MODE '{value}' is not supported. Use 'managed' or 'external'."
    )


def _parse_timeout(env: Mapping[str, str]) -> Tuple[float, Optional[str]]:
    """Return the backend call timeout in seconds, or the default plus a reason."""
    raw = _get(env, "VECTOR_SEARCH_TIMEOUT_SECONDS")
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS, None
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if value > 0:
        return value, None
    return DEFAULT_TIMEOUT_SECONDS, (
        f"VECTOR_SEARCH_TIMEOUT_SECONDS '{raw}' must be a positive number."
    )


def _default_model(provider: str) -> str:
    """Return the default model name for *provider* (none for remote providers)."""
    return DEFAULT_MODEL if provider == PROVIDER_LOCAL else ""


def _embedding_credentials(
    env: Mapping[str, str],
) -> Tuple[Optional[str], Optional[str]]:
    """Return the embeddings base URL and key, reusing LLM_* only for OpenAI."""
    reuse_llm = _get(env, "LLM_PROVIDER", "openai").lower() == "openai"
    base_url = _get(env, "EMBEDDING_BASE_URL")
    api_key = _get(env, "EMBEDDING_API_KEY")
    if reuse_llm:
        base_url = base_url or _get(env, "LLM_BASE_URL")
        api_key = api_key or _get(env, "LLM_API_KEY")
    return base_url or None, api_key or None


def _mongo_database(url: str, backend: str) -> str:
    """Return the MongoDB database named in the URL path, or the default."""
    if backend != BACKEND_MONGODB:
        return DEFAULT_MONGO_DATABASE
    name = urlsplit(url).path.strip("/")
    return name or DEFAULT_MONGO_DATABASE


def _combination_error(settings: VectorSearchSettings) -> Optional[str]:
    """Return the reason the provider, backend and names cannot work together."""
    if settings.backend == BACKEND_POSTGRES and not IDENTIFIER_RE.match(settings.table):
        return (
            f"VECTOR_SEARCH_TABLE '{settings.table}' must be 'table' or "
            "'schema.table' made of letters, digits and underscores."
        )
    if settings.backend == BACKEND_HTTP:
        return None
    return _provider_error(settings)


def _provider_error(settings: VectorSearchSettings) -> Optional[str]:
    """Return the reason the embedding provider settings are unusable."""
    if settings.provider not in PROVIDERS:
        return (
            f"EMBEDDING_PROVIDER '{settings.provider}' is not supported. "
            "Use 'local', 'openai' or 'none'."
        )
    if settings.provider == PROVIDER_NONE:
        return (
            "EMBEDDING_PROVIDER=none requires the HTTP retriever "
            "(an http(s):// VECTOR_SEARCH_URL that is not a .parquet file)."
        )
    if settings.provider != PROVIDER_OPENAI:
        return None
    if not settings.model:
        return "EMBEDDING_MODEL is required when EMBEDDING_PROVIDER=openai."
    if not settings.embedding_api_key:
        return (
            "EMBEDDING_API_KEY (or LLM_API_KEY when LLM_PROVIDER=openai) is "
            "required when EMBEDDING_PROVIDER=openai."
        )
    return _base_url_error(settings.embedding_base_url)


def _base_url_error(base_url: Optional[str]) -> Optional[str]:
    """Return why the embeddings base URL is unsafe, as llm_client judges it."""
    if not base_url:
        return None
    from llm_client import base_url_problem

    return base_url_problem(base_url, "EMBEDDING_BASE_URL")
