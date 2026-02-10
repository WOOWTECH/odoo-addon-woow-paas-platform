"""OpenAI-compatible HTTP client for AI providers.

This module provides a plain Python class (NOT an Odoo model) that wraps
HTTP calls to any OpenAI-compatible chat completion API, including support
for streaming (SSE) responses.
"""
import json
import logging
from typing import Generator, List, Optional

import requests
from requests.exceptions import ConnectionError, RequestException, Timeout

_logger = logging.getLogger(__name__)

# Default timeout for non-streaming requests (seconds)
DEFAULT_TIMEOUT = 60
# Timeout for streaming requests (seconds) -- longer to allow generation
STREAM_TIMEOUT = 300


class AIClientError(Exception):
    """Base exception for AI client errors."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        detail: Optional[str] = None,
    ):
        self.message = message
        self.status_code = status_code
        self.detail = detail
        super().__init__(self.message)


class AIClientConnectionError(AIClientError):
    """Raised when connection to the AI provider fails."""
    pass


class AIClientTimeoutError(AIClientError):
    """Raised when the AI provider request times out."""
    pass


class AIClientAPIError(AIClientError):
    """Raised when the AI provider returns an error response."""
    pass


class AIClient:
    """OpenAI-compatible HTTP client for chat completions.

    Supports any API that follows the OpenAI chat completion format,
    including OpenAI, Azure OpenAI, Ollama, and other compatible services.

    Example::

        client = AIClient(
            api_base_url="https://api.openai.com/v1",
            api_key="sk-...",
            model_name="gpt-4o",
        )
        response = client.chat_completion([
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello!"},
        ])
    """

    def __init__(
        self,
        api_base_url: str,
        api_key: str,
        model_name: str,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ):
        """Initialize the AI client.

        Args:
            api_base_url: Base URL of the OpenAI-compatible API.
            api_key: API key for authentication.
            model_name: Model identifier (e.g., "gpt-4o", "llama3").
            max_tokens: Maximum number of tokens in the response.
            temperature: Sampling temperature (0.0-1.0).
        """
        self.api_base_url = api_base_url.rstrip("/")
        self.api_key = api_key
        self.model_name = model_name
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })

    def build_messages(
        self,
        system_prompt: str,
        history: List[dict],
        user_message: str,
    ) -> List[dict]:
        """Build a messages array for the chat completion API.

        Assembles the final list of messages from a system prompt,
        conversation history, and the latest user message.

        Args:
            system_prompt: The system-level instruction for the AI.
            history: Previous conversation messages, each with
                     "role" and "content" keys.
            user_message: The latest user message to respond to.

        Returns:
            A list of message dicts ready for the chat completion API.
        """
        messages: List[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        for msg in history:
            messages.append({
                "role": msg.get("role", "user"),
                "content": msg.get("content", ""),
            })
        messages.append({"role": "user", "content": user_message})
        return messages

    def chat_completion(self, messages: List[dict]) -> str:
        """Send a chat completion request and return the full response.

        Args:
            messages: List of message dicts with "role" and "content".

        Returns:
            The assistant response text.

        Raises:
            AIClientConnectionError: If the connection fails.
            AIClientTimeoutError: If the request times out.
            AIClientAPIError: If the API returns an error.
        """
        url = f"{self.api_base_url}/chat/completions"
        payload = {
            "model": self.model_name,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": False,
        }

        try:
            response = self._session.post(
                url,
                json=payload,
                timeout=DEFAULT_TIMEOUT,
            )
        except ConnectionError as exc:
            raise AIClientConnectionError(
                f"Failed to connect to AI provider at {self.api_base_url}",
                detail=str(exc),
            ) from exc
        except Timeout as exc:
            raise AIClientTimeoutError(
                f"Request to AI provider timed out after {DEFAULT_TIMEOUT}s",
                detail=str(exc),
            ) from exc
        except RequestException as exc:
            raise AIClientError(
                f"Unexpected error communicating with AI provider: {exc}",
                detail=str(exc),
            ) from exc

        if response.status_code != 200:
            detail = response.text[:500] if response.text else None
            raise AIClientAPIError(
                f"AI provider returned HTTP {response.status_code}",
                status_code=response.status_code,
                detail=detail,
            )

        try:
            data = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise AIClientAPIError(
                "AI provider returned invalid JSON",
                detail=str(exc),
            ) from exc

        choices = data.get("choices", [])
        if not choices:
            raise AIClientAPIError(
                "AI provider returned empty choices",
                detail=json.dumps(data),
            )
        return choices[0].get("message", {}).get("content", "")

    def chat_completion_stream(
        self, messages: List[dict],
    ) -> Generator[str, None, None]:
        """Send a streaming chat completion request.

        Yields text chunks as they arrive from the AI provider using
        Server-Sent Events (SSE) format.

        Args:
            messages: List of message dicts with "role" and "content".

        Yields:
            Text chunks (strings) from the assistant response.

        Raises:
            AIClientConnectionError: If the connection fails.
            AIClientTimeoutError: If the request times out.
            AIClientAPIError: If the API returns an error.
        """
        url = f"{self.api_base_url}/chat/completions"
        payload = {
            "model": self.model_name,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": True,
        }

        try:
            response = self._session.post(
                url,
                json=payload,
                timeout=STREAM_TIMEOUT,
                stream=True,
            )
        except ConnectionError as exc:
            raise AIClientConnectionError(
                f"Failed to connect to AI provider at {self.api_base_url}",
                detail=str(exc),
            ) from exc
        except Timeout as exc:
            raise AIClientTimeoutError(
                "Streaming request to AI provider timed out",
                detail=str(exc),
            ) from exc
        except RequestException as exc:
            raise AIClientError(
                f"Unexpected error communicating with AI provider: {exc}",
                detail=str(exc),
            ) from exc

        if response.status_code != 200:
            detail = response.text[:500] if response.text else None
            raise AIClientAPIError(
                f"AI provider returned HTTP {response.status_code}",
                status_code=response.status_code,
                detail=detail,
            )

        # Parse SSE stream line by line
        for line in response.iter_lines(decode_unicode=True):
            if not line:
                continue
            if line.startswith("data: "):
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except (json.JSONDecodeError, ValueError):
                    _logger.warning("Skipping invalid SSE chunk: %s", data_str)
                    continue
                choices = chunk.get("choices", [])
                if choices:
                    delta = choices[0].get("delta", {})
                    content = delta.get("content")
                    if content:
                        yield content
