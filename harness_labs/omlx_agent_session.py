"""Tool-incapable oMLX implementation of the AgentSession contract."""

from __future__ import annotations

from dataclasses import dataclass, field

from .agent_sessions import (
    TOOL_UNAVAILABLE_REFUSAL,
    BackendCapabilities,
    BackendFailure,
    FinalOutput,
    ModelEvent,
    ModelRequest,
    ToolResult,
)
from .backends import OmlxBackend
from .text_executor import TextBackendError


@dataclass
class OmlxAgentSession:
    """Adapt local text completion to the same session boundary, without tools."""

    backend: OmlxBackend = field(
        default_factory=lambda: OmlxBackend(max_tokens=32, temperature=0.0)
    )
    _request: ModelRequest | None = field(default=None, init=False, repr=False)
    _completed: bool = field(default=False, init=False, repr=False)
    _session_id: str = field(default="omlx:single-turn", init=False, repr=False)

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            persistent_sessions=False,
            native_tool_calls=False,
            resumable_sessions=False,
            cached_input_reporting=False,
            structured_output=False,
        )

    def open(self, request: ModelRequest) -> str:
        if self._request is not None:
            raise RuntimeError("session is already open")
        if request.tools:
            raise RuntimeError("oMLX session cannot expose tools")
        self._request = request
        self._completed = False
        return self._session_id

    def step(
        self,
        session_id: str,
        tool_result: ToolResult | None = None,
    ) -> ModelEvent:
        if self._request is None or session_id != self._session_id:
            return BackendFailure("unknown oMLX session identity")
        if tool_result is not None:
            return BackendFailure("oMLX does not accept tool results")
        if self._completed:
            return BackendFailure("oMLX session is already complete")
        self._completed = True
        refusal = self._request.unavailable_tool_response
        task = (
            f"{self._request.task}\n\n"
            "You have no filesystem access and no tools. The task requires reading "
            "a file, so return exactly this sentence and nothing else:\n"
            f"{refusal}"
        )
        try:
            text = self.backend.generate(task, self._request.context).strip()
        except TextBackendError as exc:
            return BackendFailure(str(exc))
        if text != TOOL_UNAVAILABLE_REFUSAL:
            return BackendFailure("oMLX did not return the required refusal")
        return FinalOutput(
            text,
            evidence=("omlx-transport:openai-compatible-http",),
        )

    def close(self, session_id: str) -> None:
        if self._request is not None and session_id != self._session_id:
            raise RuntimeError("unknown oMLX session identity")
        self._request = None
        self._completed = False
