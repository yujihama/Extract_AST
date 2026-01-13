from __future__ import annotations

import json
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from compare_app.contracts import CancellationToken, EventSink
from compare_app.core.pipeline import CancelledError


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate(s: str, n: int = 300) -> str:
    t = s if isinstance(s, str) else str(s)
    return t if len(t) <= n else (t[:n] + "…")


def _generate_invocation_id() -> str:
    """短いユニークな呼び出しIDを生成する。"""
    return uuid.uuid4().hex[:12]


# エージェント呼び出しスタックを追跡するContextVar
# 各要素は (invocation_id, agent_name) のタプル
_agent_stack: ContextVar[list[tuple[str, str]]] = ContextVar("agent_stack", default=[])


class AgentInvocationContext:
    """エージェント呼び出しの親子関係を追跡するコンテキストマネージャー。
    
    使用例:
        with AgentInvocationContext.push("inv_abc123", "my_agent"):
            # この間、子エージェントは親を取得できる
            parent = AgentInvocationContext.current_parent()
    """
    
    @classmethod
    def push(
        cls,
        invocation_id: str,
        agent_name: str,
        *,
        prefix: Optional[list[tuple[str, str]]] = None,
    ) -> "AgentInvocationContext":
        """新しいエージェント呼び出しをスタックにプッシュ。"""
        return cls(invocation_id, agent_name, prefix=prefix)
    
    @classmethod
    def current_parent(cls) -> tuple[Optional[str], Optional[str]]:
        """現在のスタックトップ（親エージェント）の情報を取得。
        
        Returns:
            (parent_invocation_id, parent_agent_name) または (None, None)
        """
        stack = _agent_stack.get()
        if stack:
            return stack[-1]
        return (None, None)
    
    @classmethod
    def get_stack_depth(cls) -> int:
        """現在のスタック深度を取得。"""
        return len(_agent_stack.get())
    
    def __init__(self, invocation_id: str, agent_name: str, *, prefix: Optional[list[tuple[str, str]]] = None):
        self._invocation_id = invocation_id
        self._agent_name = agent_name
        self._prefix = prefix or []
        self._token = None
    
    def __enter__(self) -> "AgentInvocationContext":
        stack = _agent_stack.get().copy()
        if self._prefix:
            stack.extend(self._prefix)
        stack.append((self._invocation_id, self._agent_name))
        self._token = _agent_stack.set(stack)
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._token is not None:
            # 直前の状態へ確実に戻す（thread/context跨ぎの補助にもなる）
            _agent_stack.reset(self._token)
        return None


@dataclass
class EventSinkMiddleware(AgentMiddleware):
    """deep_agent / subagent の実行過程を EventSink へ流す。

    目的:
    - UI（SSE）で tool/subagent の流れを追えるようにする
    - JSONL tail などの追加仕組み無しでリアルタイム化
    - 親子関係を追跡可能にする（invocation_id / parent_invocation_id）
    """

    run_id: str
    events: EventSink
    cancellation: Optional[CancellationToken] = None
    agent_name: str = "agent"
    is_subagent: bool = False

    # thread/context を跨ぐ subagent 実行でも親子関係を保つためのヒント
    forced_parent_invocation_id: Optional[str] = None
    forced_parent_agent_name: Optional[str] = None
    
    # 呼び出しIDは自動生成（インスタンス生成時に確定）
    invocation_id: str = field(default_factory=_generate_invocation_id)
    
    # 親の呼び出しID（before_agent時に自動取得）
    _parent_invocation_id: Optional[str] = field(default=None, init=False, repr=False)
    _parent_agent_name: Optional[str] = field(default=None, init=False, repr=False)
    _context: Optional[AgentInvocationContext] = field(default=None, init=False, repr=False)

    def _raise_if_cancelled(self) -> None:
        if self.cancellation is not None and self.cancellation.is_cancelled():
            raise CancelledError(f"cancelled during agent execution: {self.agent_name}")

    def _get_hierarchy_info(self) -> dict[str, Any]:
        """親子関係の情報を含む辞書を返す。"""
        return {
            "invocation_id": self.invocation_id,
            "parent_invocation_id": self._parent_invocation_id,
            "parent_agent_name": self._parent_agent_name,
            "depth": AgentInvocationContext.get_stack_depth(),
        }

    def before_agent(self, state: AgentState, runtime) -> Optional[dict]:
        self._raise_if_cancelled()
        
        # 親エージェントの情報を取得（スタックから）
        parent_inv_id, parent_name = AgentInvocationContext.current_parent()
        prefix = None
        if parent_inv_id is None and self.forced_parent_invocation_id:
            # contextvar が伝播しない実行経路（例: tool(task) で subagent 実行）向け
            parent_inv_id = self.forced_parent_invocation_id
            parent_name = self.forced_parent_agent_name
            # 表示用 depth を自然にするため、スタックに親を“前置き”する
            prefix = [(parent_inv_id, parent_name or "agent")]
        self._parent_invocation_id = parent_inv_id
        self._parent_agent_name = parent_name
        
        # このエージェントをスタックにプッシュ
        self._context = AgentInvocationContext.push(self.invocation_id, self.agent_name, prefix=prefix)
        self._context.__enter__()
        
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
                **self._get_hierarchy_info(),
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
                **self._get_hierarchy_info(),
            },
        )
        
        # スタックからポップ
        if self._context is not None:
            self._context.__exit__(None, None, None)
            self._context = None
        
        return None

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Any]) -> Any:
        self._raise_if_cancelled()
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
                **self._get_hierarchy_info(),
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
                    **self._get_hierarchy_info(),
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
                    **self._get_hierarchy_info(),
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

