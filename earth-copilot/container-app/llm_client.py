import os
from typing import Any, Dict, List, Optional

import logging

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


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
        else:
            # For now only OpenAI is supported; other providers can be added
            raise LLMConfigurationError(
                f"Unsupported LLM_PROVIDER '{provider}'. Only 'openai' is currently supported."
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
                # Let the model choose tools automatically when provided
                params["tool_choice"] = "auto"

            # Allow callers to override or extend parameters
            params.update(kwargs)

            response = await self._client.chat.completions.create(**params)
            # Return a plain dict so callers are not tied to OpenAI's types
            return response.model_dump()

        # Fallback safety – should not be reachable because __init__ guards provider
        raise LLMConfigurationError(f"Unsupported provider '{self.provider}'")


# Convenience module-level helper used by the app
_global_client: Optional[LLMClient] = None


def get_llm_client() -> LLMClient:
    global _global_client
    if _global_client is None:
        _global_client = LLMClient.from_env()
    return _global_client
