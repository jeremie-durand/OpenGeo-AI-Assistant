import os
from typing import Any, Dict, List, Optional
import httpx
import logging

logger = logging.getLogger(__name__)

class AnthropicClient:
    """Minimal async client for Anthropic's Claude API (v2023-06-01)."""
    def __init__(self, api_key: str, model: str, base_url: Optional[str] = None):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url or "https://api.anthropic.com/v1"
        self.headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }

    async def chat(self, messages: List[Dict[str, str]], **kwargs: Any) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": kwargs.get("max_tokens", 1024),
            "temperature": kwargs.get("temperature", 0.7),
            "messages": messages,
        }
        # Anthropic rejects "system" when it is an empty string
        system = kwargs.get("system", "")
        if system:
            payload["system"] = system
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/messages",
                headers=self.headers,
                json=payload,
                timeout=60
            )
            if response.status_code >= 400:
                logger.error(
                    f"[Anthropic] {response.status_code} — {response.text}"
                )
            response.raise_for_status()
            return response.json()

    def _messages_to_prompt(self, messages: List[Dict[str, str]]) -> str:
        # Convert OpenAI-style messages to Anthropic prompt format if needed
        # For Claude v2, you can pass messages directly
        return ""  # Not used in v2023-06-01
