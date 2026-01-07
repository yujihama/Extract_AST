from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from compare_app.contracts import EventSink


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate(s: str, n: int = 300) -> str:
    t = s if isinstance(s, str) else str(s)
    return t if len(t) <= n else (t[:n] + "…")


@dataclass
class EventSinkMiddleware(AgentMiddleware):
    """deep_agent / subagent の実行過程を EventSink へ流す（案A）。

    目的:
    - UI（SSE）で tool/subagent の流れを追えるようにする
    - JSONL tail などの追加仕組み無しでリアルタイム化
    """

    run_id: str
    events: EventSink
    agent_name: str = "agent"
    is_subagent: bool = False

    def before_agent(self, state: AgentState, runtime) -> Optional[dict]:
        messages = state.get("messages", [])
        last = messages[-1:] if messages else []
        self.events.emit(
            self.run_id,
            "agent_start",
            {
                "ts": _utcnow_iso(),
                "agent_name": self.agent_name,
                "is_subagent": bool(self.is_subagent),
                "message_count": len(messages),
                "last_message": self._summarize_messages(last),
            },
        )
        return None

    def after_agent(self, state: AgentState, runtime) -> Optional[dict]:
        messages = state.get("messages", [])
        final_content = None
        for m in reversed(messages):
            if isinstance(m, AIMessage) and m.content and not m.tool_calls:
                final_content = m.content
                break
        self.events.emit(
            self.run_id,
            "agent_end",
            {
                "ts": _utcnow_iso(),
                "agent_name": self.agent_name,
                "is_subagent": bool(self.is_subagent),
                "total_messages": len(messages),
                "final_response_preview": _truncate(final_content or "", 500) if final_content else None,
            },
        )
        return None

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Any]) -> Any:
        tool_call = request.tool_call
        tool_name = tool_call.get("name", "unknown")
        tool_args = tool_call.get("args", {}) or {}
        tool_call_id = tool_call.get("id", "")

        self.events.emit(
            self.run_id,
            "tool_call_start",
            {
                "ts": _utcnow_iso(),
                "agent_name": self.agent_name,
                "is_subagent": bool(self.is_subagent),
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "args_keys": list(tool_args.keys()) if isinstance(tool_args, dict) else None,
            },
        )
        try:
            result = handler(request)
            # ToolMessageの場合はpreviewを作る
            preview = None
            if hasattr(result, "content"):
                content = result.content if isinstance(result.content, str) else str(result.content)
                preview = _truncate(content, 500)
            self.events.emit(
                self.run_id,
                "tool_call_result",
                {
                    "ts": _utcnow_iso(),
                    "agent_name": self.agent_name,
                    "is_subagent": bool(self.is_subagent),
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "status": "success",
                    "result_preview": preview,
                },
            )
            return result
        except Exception as e:
            self.events.emit(
                self.run_id,
                "tool_call_error",
                {
                    "ts": _utcnow_iso(),
                    "agent_name": self.agent_name,
                    "is_subagent": bool(self.is_subagent),
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "status": "error",
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
            )
            raise

    def _summarize_messages(self, messages: list) -> list:
        summary = []
        for m in messages:
            if isinstance(m, HumanMessage):
                summary.append({"type": "human", "content": _truncate(m.content or "", 200)})
            elif isinstance(m, AIMessage):
                entry = {"type": "ai", "content": _truncate(m.content or "", 200) if m.content else None}
                if m.tool_calls:
                    entry["tool_calls"] = [{"name": tc.get("name"), "args_keys": list((tc.get("args") or {}).keys())} for tc in m.tool_calls]
                summary.append(entry)
            elif isinstance(m, ToolMessage):
                content = m.content if isinstance(m.content, str) else str(m.content)
                summary.append({"type": "tool_result", "tool_call_id": m.tool_call_id, "content": _truncate(content, 200)})
        return summary

