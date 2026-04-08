"""LLMClient configuration and instantiation smoke tests."""

import pytest
from llm_client import (
    LLMClient,
    LLMConfigurationError,
    OpenAICompatClient,
    get_llm_client,
)


def test_openai_client_instantiation() -> None:
    c = LLMClient(provider="openai", api_key="fake", model="gpt-4")
    assert c.provider == "openai"


def test_from_env_reads_env_vars() -> None:
    import llm_client as _lc

    _lc._global_client = None
    c = LLMClient.from_env()
    assert c.provider == "openai"
    assert c.model == "gpt-4"


def test_missing_api_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    with pytest.raises(LLMConfigurationError, match="LLM_API_KEY"):
        LLMClient.from_env()


def test_missing_model_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_MODEL", raising=False)
    with pytest.raises(LLMConfigurationError, match="LLM_MODEL"):
        LLMClient.from_env()


def test_unsupported_provider_raises() -> None:
    with pytest.raises(LLMConfigurationError, match="Unsupported"):
        LLMClient(provider="bedrock", api_key="x", model="x")


def test_openai_compat_client() -> None:
    inner = LLMClient(provider="openai", api_key="fake", model="gpt-4")
    compat = OpenAICompatClient(inner)
    assert compat.provider == "openai"
    assert hasattr(compat.chat, "completions")


def test_get_llm_client_returns_compat() -> None:
    assert isinstance(get_llm_client(), OpenAICompatClient)


def test_anthropic_client_instantiation() -> None:
    from anthropic_client import AnthropicClient

    c = AnthropicClient(api_key="fake", model="claude-3-5-sonnet-20241022")
    assert c.model == "claude-3-5-sonnet-20241022"
