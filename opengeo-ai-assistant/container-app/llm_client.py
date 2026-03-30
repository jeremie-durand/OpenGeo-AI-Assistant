
import os
from typing import Any, Dict, List, Optional
import logging

from openai import AsyncOpenAI
from anthropic_client import AnthropicClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# OpenAI-compatible shim for Anthropic responses
# ---------------------------------------------------------------------------

class _Message:
    def __init__(self, content: str, role: str = "assistant"):
        self.content = content
        self.role = role

class _Choice:
    def __init__(self, content: str):
        self.message = _Message(content)
        self.finish_reason = "stop"
        self.index = 0

class _CompletionResponse:
    def __init__(self, content: str, model: str):
        self.choices = [_Choice(content)]
        self.model = model
        self.object = "chat.completion"

    def model_dump(self) -> Dict[str, Any]:
        return {
            "choices": [{"message": {"content": c.message.content, "role": c.message.role}, "finish_reason": c.finish_reason} for c in self.choices],
            "model": self.model,
            "object": self.object,
        }


class _CompletionsProxy:
    """Exposes `.create()` using an underlying LLMClient."""

    def __init__(self, llm_client: "LLMClient"):
        self._llm = llm_client

    async def create(
        self,
        model: Optional[str] = None,
        messages: Optional[List[Dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> Any:
        messages = messages or []
        if self._llm.provider == "openai":
            # Delegate straight to the raw AsyncOpenAI client so callers get a
            # real OpenAI response object (with .choices[0].message.content).
            params: Dict[str, Any] = {"model": model or self._llm.model, "messages": messages}
            params.update(kwargs)
            return await self._llm._client.chat.completions.create(**params)
        else:
            # Anthropic — call through LLMClient.chat() and wrap the response.
            raw = await self._llm.chat(messages, **kwargs)
            # Extract text from Anthropic response format.
            content_blocks = raw.get("content", [])
            if content_blocks and isinstance(content_blocks, list):
                text = content_blocks[0].get("text", "") if isinstance(content_blocks[0], dict) else str(content_blocks[0])
            else:
                text = str(raw)
            return _CompletionResponse(text, model or self._llm.model)


class _ChatProxy:
    def __init__(self, llm_client: "LLMClient"):
        self.completions = _CompletionsProxy(llm_client)


class OpenAICompatClient:
    """Wraps an LLMClient to expose the OpenAI `.chat.completions.create()` interface.

    This lets every call-site that was written for OpenAI work with any provider supported by LLMClient, without needing to
    """

    def __init__(self, llm_client: "LLMClient"):
        self._llm = llm_client
        self.chat = _ChatProxy(llm_client)
        # Expose provider/model for informational use
        self.provider = llm_client.provider
        self.model = llm_client.model


class LLMConfigurationError(RuntimeError):
    pass


class LLMClient:
    """Provider-agnostic LLM chat client.

    Configuration is read from environment variables:
    - LLM_PROVIDER:   e.g. "openai"
    - LLM_API_KEY:    API key/token for the provider
    - LLM_MODEL:      model identifier (e.g. "gpt-4.1-mini")
    - LLM_BASE_URL:   optional base URL override
    - LLM_API_VERSION: optional API version (provider specific)
    - LLM_ORG_ID:     optional organization/tenant identifier
    """

    def __init__(
        self,
        provider: str,
        api_key: str,
        model: str,
        base_url: Optional[str] = None,
        api_version: Optional[str] = None,
        org_id: Optional[str] = None,
    ) -> None:
        self.provider = provider.lower().strip()
        self.model = model
        self.api_version = api_version

        if self.provider == "openai":
            client_kwargs: Dict[str, Any] = {"api_key": api_key}
            if base_url:
                client_kwargs["base_url"] = base_url
            if org_id:
                client_kwargs["organization"] = org_id
            self._client = AsyncOpenAI(**client_kwargs)
        elif self.provider == "anthropic":
            self._client = AnthropicClient(api_key=api_key, model=model, base_url=base_url)
        else:
            raise LLMConfigurationError(
                f"Unsupported LLM_PROVIDER '{provider}'. Only 'openai' and 'anthropic' are supported."
            )

    @classmethod
    def from_env(cls) -> "LLMClient":
        provider = os.getenv("LLM_PROVIDER", "openai").strip() or "openai"
        api_key = os.getenv("LLM_API_KEY", "").strip()
        model = os.getenv("LLM_MODEL", "").strip()
        base_url = os.getenv("LLM_BASE_URL", "").strip() or None
        api_version = os.getenv("LLM_API_VERSION", "").strip() or None
        org_id = os.getenv("LLM_ORG_ID", "").strip() or None

        missing: List[str] = []
        if not api_key:
            missing.append("LLM_API_KEY")
        if not model:
            missing.append("LLM_MODEL")

        if missing:
            raise LLMConfigurationError(
                "Missing required LLM configuration: " + ", ".join(missing)
            )

        logger.info(
            "[LLM] Using provider=%s model=%s base_url=%s", provider, model, base_url or "<default>"
        )
        return cls(
            provider=provider,
            api_key=api_key,
            model=model,
            base_url=base_url,
            api_version=api_version,
            org_id=org_id,
        )

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Send a chat completion request.

        `messages` is expected to be an OpenAI-style list of {"role","content"}.
        `tools` (if provided) is forwarded as-is for providers that support it.
        """

        if self.provider == "openai":
            params: Dict[str, Any] = {
                "model": self.model,
                "messages": messages,
            }
            if tools:
                params["tools"] = tools
                params["tool_choice"] = "auto"
            params.update(kwargs)
            response = await self._client.chat.completions.create(**params)
            return response.model_dump()
        elif self.provider == "anthropic":
            # AnthropicClient expects OpenAI-style messages, returns Claude response
            response = await self._client.chat(messages, **kwargs)
            return response
        raise LLMConfigurationError(f"Unsupported provider '{self.provider}'")


# Convenience module-level helper used by the app
_global_client: Optional[LLMClient] = None


def get_llm_client(model: Optional[str] = None, vision: bool = False) -> OpenAICompatClient:
    """Return an OpenAI-compatible client for the configured provider.

    The `model` and `vision` parameters are accepted for API compatibility with
    call-sites that pass them, but the provider and credentials always come from
    environment variables (LLM_PROVIDER / LLM_API_KEY / LLM_MODEL).
    """
    global _global_client
    if _global_client is None:
        _global_client = LLMClient.from_env()
    return OpenAICompatClient(_global_client)
