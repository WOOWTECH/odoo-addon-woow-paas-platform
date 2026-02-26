"""AI client powered by LangChain's ChatOpenAI.

This module wraps ``langchain-openai``'s :class:`ChatOpenAI` to provide a
simple interface for chat completions (both synchronous and streaming)
against any OpenAI-compatible API endpoint.

Supports optional MCP tool calling via ``langchain-mcp-adapters`` and
LangGraph's ``StateGraph`` with ``ToolNode``.
"""
import asyncio
import json
import logging
from typing import Generator, List, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# The AI model will respond in its default format (typically Markdown).
# The frontend will handle conversion to HTML for display.
# ---------------------------------------------------------------------------


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

    @classmethod
    def from_assistant(cls, assistant):
        """Create an AIClient from an ``ai.assistant`` record.

        Reads configuration from ``assistant.config_id`` (an ``ai.config``
        record) and builds a LangChain client.  The ``api_base_url`` field
        comes from our custom extension of ``ai.config``.

        Args:
            assistant: An ``ai.assistant`` Odoo recordset (single record).

        Returns:
            A configured :class:`AIClient` instance.

        Raises:
            AIClientError: If the assistant has no configuration or is missing
                a required API key.
        """
        config = assistant.sudo().config_id
        if not config:
            raise AIClientError("AI assistant has no configuration")
        api_key = config.api_key
        if not api_key:
            raise AIClientError("AI configuration is missing an API key")
        return cls(
            api_base_url=config.api_base_url or 'https://api.openai.com/v1',
            api_key=api_key,
            model_name=config.model or 'gpt-4o-mini',
            max_tokens=config.max_tokens or 4096,
            temperature=config.temperature if config.temperature is not None else 0.7,
        )

    # -------------------- message helpers --------------------

    def build_messages(
        self,
        system_prompt: str,
        history: List[dict],
        user_message: str,
    ) -> list:
        """Build a LangChain message list for a chat completion call.

        The model will respond in its default format (typically Markdown).
        """
        messages: list = []

        if system_prompt and system_prompt.strip():
            messages.append(SystemMessage(content=system_prompt.strip()))

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

    # -------------------- tool calling --------------------

    def chat_completion_with_tools(
        self,
        messages: list,
        mcp_tools=None,
    ) -> str:
        """Chat completion with optional MCP tool calling.

        Uses LangGraph agent to handle the tool-call loop. Falls back to
        plain ``chat_completion`` when no tools are provided or on failure.

        Args:
            messages: LangChain message list.
            mcp_tools: Odoo recordset of ``woow_paas_platform.mcp_tool``.

        Returns:
            The final assistant response text.
        """
        if not mcp_tools:
            return self.chat_completion(messages)
        server_config = _build_mcp_server_config(mcp_tools)
        enabled_names = {t.name for t in mcp_tools}
        try:
            return asyncio.run(
                self._async_agent_invoke(messages, server_config, enabled_names)
            )
        except Exception as exc:
            _logger.warning("Tool calling failed, falling back to text-only: %s", exc)
            return self.chat_completion(messages)

    def chat_completion_stream_with_tools(
        self,
        messages: list,
        mcp_tools=None,
    ) -> Generator[dict, None, None]:
        """Streaming chat completion with optional MCP tool calling.

        Yields event dicts with ``type`` key:

        - ``{"type": "text_chunk", "content": "..."}``
        - ``{"type": "tool_call", "tool": "name", "args": {...}}``
        - ``{"type": "tool_result", "tool": "name", "result": "..."}``

        Falls back to text-only streaming when no tools or on failure.

        Args:
            messages: LangChain message list.
            mcp_tools: Odoo recordset of ``woow_paas_platform.mcp_tool``.
        """
        if not mcp_tools:
            for chunk in self.chat_completion_stream(messages):
                yield {"type": "text_chunk", "content": chunk}
            return

        server_config = _build_mcp_server_config(mcp_tools)
        enabled_names = {t.name for t in mcp_tools}
        try:
            events = asyncio.run(
                self._async_agent_stream(messages, server_config, enabled_names)
            )
            yield from events
        except Exception as exc:
            _logger.warning(
                "Tool calling stream failed, falling back to text-only: %s", exc
            )
            for chunk in self.chat_completion_stream(messages):
                yield {"type": "text_chunk", "content": chunk}

    # MCP agent timeout (seconds) to prevent hanging on unreachable servers
    _MCP_TIMEOUT = 60
    _RECURSION_LIMIT = 5

    async def _async_agent_invoke(self, messages, server_config, enabled_names):
        """Build LangGraph agent, invoke, and return final text.

        Handles connection failures, timeouts, and recursion limits gracefully
        by falling back to plain chat completion.
        """
        from langchain_mcp_adapters.client import MultiServerMCPClient

        try:
            async with MultiServerMCPClient(server_config) as client:
                tools = await asyncio.wait_for(
                    client.get_tools(), timeout=self._MCP_TIMEOUT,
                )
                tools = [t for t in tools if t.name in enabled_names]
                if not tools:
                    result = self.llm.invoke(messages)
                    return result.content
                graph = self._build_agent_graph(tools)
                result = await asyncio.wait_for(
                    graph.ainvoke({"messages": messages}),
                    timeout=self._MCP_TIMEOUT,
                )
                return result["messages"][-1].content
        except asyncio.TimeoutError:
            _logger.warning("MCP tool calling timed out after %ss", self._MCP_TIMEOUT)
            raise
        except Exception as exc:
            exc_type = type(exc).__name__
            if "GraphRecursionError" in exc_type or "RecursionError" in exc_type:
                _logger.warning(
                    "MCP tool calling hit recursion limit (%s iterations)",
                    self._RECURSION_LIMIT,
                )
            else:
                _logger.warning("MCP agent invoke failed (%s): %s", exc_type, exc)
            raise

    async def _async_agent_stream(self, messages, server_config, enabled_names):
        """Build LangGraph agent, stream updates, return event list.

        Handles connection failures, timeouts, and recursion limits gracefully.
        On tool execution errors, emits ``tool_error`` events so the frontend
        can display error states.
        """
        from langchain_mcp_adapters.client import MultiServerMCPClient

        events = []
        try:
            async with MultiServerMCPClient(server_config) as client:
                tools = await asyncio.wait_for(
                    client.get_tools(), timeout=self._MCP_TIMEOUT,
                )
                tools = [t for t in tools if t.name in enabled_names]
                if not tools:
                    result = self.llm.invoke(messages)
                    events.append({"type": "text_chunk", "content": result.content})
                    return events
                graph = self._build_agent_graph(tools)
                async for chunk in graph.astream(
                    {"messages": messages}, stream_mode="updates"
                ):
                    for node_name, node_output in chunk.items():
                        for msg in node_output.get("messages", []):
                            if (
                                hasattr(msg, "tool_calls")
                                and msg.tool_calls
                            ):
                                for tc in msg.tool_calls:
                                    events.append({
                                        "type": "tool_call",
                                        "tool": tc["name"],
                                        "args": tc.get("args", {}),
                                    })
                            elif node_name == "tools" and hasattr(msg, "content"):
                                content = str(msg.content)
                                tool_name = getattr(msg, "name", "unknown")
                                # Detect tool execution errors
                                is_error = getattr(msg, "status", None) == "error"
                                if is_error:
                                    events.append({
                                        "type": "tool_error",
                                        "tool": tool_name,
                                        "error": content,
                                    })
                                    _logger.warning(
                                        "MCP tool '%s' execution error: %s",
                                        tool_name, content[:200],
                                    )
                                else:
                                    events.append({
                                        "type": "tool_result",
                                        "tool": tool_name,
                                        "result": content,
                                    })
                            elif (
                                node_name == "call_model"
                                and hasattr(msg, "content")
                                and msg.content
                                and not getattr(msg, "tool_calls", None)
                            ):
                                events.append({
                                    "type": "text_chunk",
                                    "content": msg.content,
                                })
        except asyncio.TimeoutError:
            _logger.warning("MCP tool stream timed out after %ss", self._MCP_TIMEOUT)
            raise
        except Exception as exc:
            exc_type = type(exc).__name__
            if "GraphRecursionError" in exc_type or "RecursionError" in exc_type:
                _logger.warning(
                    "MCP tool stream hit recursion limit (%s iterations)",
                    self._RECURSION_LIMIT,
                )
                # Emit a system notification so the frontend knows
                events.append({
                    "type": "text_chunk",
                    "content": "\n\n⚠️ Tool calling reached the maximum iteration limit. "
                               "Providing the best answer based on results gathered so far.\n",
                })
                return events
            _logger.warning("MCP agent stream failed (%s): %s", exc_type, exc)
            raise
        return events

    def _build_agent_graph(self, tools):
        """Build a LangGraph StateGraph with ToolNode for tool calling."""
        from langgraph.graph import START, MessagesState, StateGraph
        from langgraph.prebuilt import ToolNode, tools_condition

        llm_with_tools = self.llm.bind_tools(tools)

        def call_model(state):
            return {"messages": [llm_with_tools.invoke(state["messages"])]}

        builder = StateGraph(MessagesState)
        builder.add_node("call_model", call_model)
        builder.add_node("tools", ToolNode(tools))
        builder.add_edge(START, "call_model")
        builder.add_conditional_edges("call_model", tools_condition)
        builder.add_edge("tools", "call_model")
        return builder.compile(recursion_limit=self._RECURSION_LIMIT)

    # -------------------- completions --------------------

    def chat_completion(self, messages: list) -> str:
        """Send a chat completion request and return the full response.

        Returns:
            The assistant response text (typically Markdown).

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
# Internal helpers
# ---------------------------------------------------------------------------

def _build_mcp_server_config(mcp_tools):
    """Build MultiServerMCPClient config from an Odoo mcp_tool recordset.

    Groups tools by their server and returns a dict keyed by server name,
    with each value being the server's connection config.
    """
    servers = {}
    for tool in mcp_tools:
        server = tool.server_id
        if server.name not in servers:
            servers[server.name] = server._get_mcp_client_config()
    return servers


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
