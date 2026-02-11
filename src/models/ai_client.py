"""AI client powered by LangChain's ChatOpenAI.

This module wraps ``langchain-openai``'s :class:`ChatOpenAI` to provide a
simple interface for chat completions (both synchronous and streaming)
against any OpenAI-compatible API endpoint.

The public interface is intentionally kept identical to the previous
hand-rolled HTTP client so that callers do not need any changes.
"""
import logging
from typing import Generator, List, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HTML output instruction appended to every system prompt so the model
# returns well-structured HTML instead of raw Markdown.
# ---------------------------------------------------------------------------
HTML_OUTPUT_INSTRUCTION = (
    "\n\n[Output Format]\n"
    "You MUST reply in HTML. Follow these rules:\n"
    "- Wrap paragraphs in <p>.\n"
    "- Use <strong> for bold and <em> for italic.\n"
    "- Use <pre><code> for code blocks and <code> for inline code.\n"
    "- Use <ul>/<ol> with <li> for lists.\n"
    "- Use <h3> or <h4> for headings (never <h1> or <h2>).\n"
    "- Use <blockquote> for quotes.\n"
    "- Use <table>/<thead>/<tbody>/<tr>/<th>/<td> for tables.\n"
    "- Do NOT use Markdown syntax (**, ```, #, etc.).\n"
    "- Do NOT wrap output in <html>, <head>, or <body> tags."
)


# ---------------------------------------------------------------------------
# Exception hierarchy (unchanged from the previous implementation)
# ---------------------------------------------------------------------------

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


class AIClientTimeoutError(AIClientError):
    """Raised when the AI provider request times out."""


class AIClientAPIError(AIClientError):
    """Raised when the AI provider returns an error response."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class AIClient:
    """OpenAI-compatible chat client backed by LangChain.

    Supports any API that follows the OpenAI chat completion format,
    including OpenAI, Azure OpenAI, Ollama, and other compatible services.

    Example::

        client = AIClient(
            api_base_url="https://api.openai.com/v1",
            api_key="sk-...",
            model_name="gpt-4o",
        )
        response = client.chat_completion([
            SystemMessage(content="You are helpful."),
            HumanMessage(content="Hello!"),
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
        self.llm = ChatOpenAI(
            base_url=api_base_url.rstrip("/"),
            api_key=api_key,
            model=model_name,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    # -------------------- message helpers --------------------

    def build_messages(
        self,
        system_prompt: str,
        history: List[dict],
        user_message: str,
    ) -> list:
        """Build a LangChain message list for a chat completion call.

        The *HTML_OUTPUT_INSTRUCTION* is appended to *system_prompt* so
        the model replies in HTML rather than Markdown.
        """
        messages: list = []

        combined_prompt = (
            (system_prompt or "") + HTML_OUTPUT_INSTRUCTION
        ).strip()
        if combined_prompt:
            messages.append(SystemMessage(content=combined_prompt))

        for msg in history:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "assistant":
                messages.append(AIMessage(content=content))
            else:
                messages.append(HumanMessage(content=content))

        messages.append(HumanMessage(content=user_message))
        return messages

    # -------------------- completions --------------------

    def chat_completion(self, messages: list) -> str:
        """Send a chat completion request and return the full response.

        Returns:
            The assistant response text (HTML).

        Raises:
            AIClientConnectionError: Connection to the provider failed.
            AIClientTimeoutError: The request timed out.
            AIClientAPIError: The provider returned an error.
        """
        try:
            result = self.llm.invoke(messages)
            return result.content
        except Exception as exc:
            raise _translate_exception(exc) from exc

    def chat_completion_stream(
        self,
        messages: list,
    ) -> Generator[str, None, None]:
        """Stream a chat completion, yielding text chunks.

        Yields:
            Text chunks (strings) from the assistant response.

        Raises:
            AIClientConnectionError: Connection to the provider failed.
            AIClientTimeoutError: The request timed out.
            AIClientAPIError: The provider returned an error.
        """
        try:
            for chunk in self.llm.stream(messages):
                if chunk.content:
                    yield chunk.content
        except Exception as exc:
            raise _translate_exception(exc) from exc


# ---------------------------------------------------------------------------
# Internal: translate LangChain / openai exceptions into AIClient* errors
# ---------------------------------------------------------------------------

def _translate_exception(exc: Exception) -> AIClientError:
    """Map upstream exceptions to the AIClient* hierarchy."""
    exc_type = type(exc).__name__
    exc_module = type(exc).__module__ or ""
    detail = str(exc)

    # openai library errors (re-exported via langchain-openai)
    if "openai" in exc_module:
        if "APIConnectionError" in exc_type:
            return AIClientConnectionError(
                "Failed to connect to AI provider",
                detail=detail,
            )
        if "Timeout" in exc_type or "APITimeoutError" in exc_type:
            return AIClientTimeoutError(
                "Request to AI provider timed out",
                detail=detail,
            )
        # AuthenticationError, RateLimitError, BadRequestError, etc.
        status_code = getattr(exc, "status_code", None)
        return AIClientAPIError(
            f"AI provider error: {exc_type}",
            status_code=status_code,
            detail=detail,
        )

    # httpx / requests connection errors
    if "ConnectError" in exc_type or "ConnectionError" in exc_type:
        return AIClientConnectionError(
            "Failed to connect to AI provider",
            detail=detail,
        )

    if "Timeout" in exc_type or "ReadTimeout" in exc_type:
        return AIClientTimeoutError(
            "Request to AI provider timed out",
            detail=detail,
        )

    # Fallback
    return AIClientError(
        f"Unexpected AI client error: {exc_type}",
        detail=detail,
    )
