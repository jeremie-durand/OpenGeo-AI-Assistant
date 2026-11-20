"""Semantic Kernel initialization in SemanticQueryTranslator."""

import pytest
from semantic_translator import SemanticQueryTranslator

LOCAL_BASE_URL = "http://localhost:11434/v1/"


@pytest.mark.asyncio
async def test_kernel_initializes_with_both_chat_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both chat services register and target the configured LLM_BASE_URL."""
    monkeypatch.setenv("LLM_BASE_URL", LOCAL_BASE_URL)
    translator = SemanticQueryTranslator()

    await translator._ensure_kernel_initialized()

    assert translator._kernel_initialized is True
    for service_id in ("chat-completion", "chat-completion-4o"):
        service = translator.kernel.get_service(service_id)
        assert str(service.client.base_url) == LOCAL_BASE_URL


@pytest.mark.asyncio
async def test_kernel_initializes_without_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unset LLM_BASE_URL falls back to the SDK's default endpoint."""
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    translator = SemanticQueryTranslator()

    await translator._ensure_kernel_initialized()

    assert translator._kernel_initialized is True
