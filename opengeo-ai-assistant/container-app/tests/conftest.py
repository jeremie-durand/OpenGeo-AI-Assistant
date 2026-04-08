"""Shared fixtures: env patching and LLM singleton reset."""
import sys
from pathlib import Path

import pytest

CONTAINER_APP = Path(__file__).parent.parent
if str(CONTAINER_APP) not in sys.path:
    sys.path.insert(0, str(CONTAINER_APP))

FAKE_ENV = {
    "LLM_PROVIDER": "openai",
    "LLM_API_KEY": "test-key-not-real",
    "LLM_MODEL": "gpt-4",
    "BACKEND_URL": "http://localhost:8000",
    "ENABLE_AUTH": "false",
    "RATE_LIMIT_LLM": "1000/minute",
    "RATE_LIMIT_SEARCH": "1000/minute",
}


@pytest.fixture(autouse=True)
def patch_env(monkeypatch):
    for key, value in FAKE_ENV.items():
        monkeypatch.setenv(key, value)


@pytest.fixture(autouse=True)
def reset_llm_singleton():
    import llm_client as _lc

    _lc._global_client = None
    yield
    _lc._global_client = None
