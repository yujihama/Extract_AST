from __future__ import annotations

import json
import os
import threading
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
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


@dataclass(frozen=True)
class CompareRestoreConfig:
    """
    `compare_*` ツール呼び出し時に、必要なら compare_setup を再実行して
    スレッドローカルな COMPARE_STATE を“このスレッド”で初期化するための設定。
    """

    ast_a_path: str
    ast_b_path: str
    cache_path: str
    txt_a_path: Optional[str] = None
    txt_b_path: Optional[str] = None
    initial_matching_path: str = ""
    embedding_model: Optional[str] = None
    embedding_batch_size: int = 64
    warmup_matching: bool = False
    match_top_k: int = 3
    match_alpha: float = 0.3
    match_beta: float = 0.4
    match_min_score: float = 0.25


_COMPARE_RESTORE_LOCKS: dict[str, threading.Lock] = {}
_COMPARE_RESTORE_LOCKS_GUARD = threading.Lock()


def _get_compare_restore_lock(run_id: str) -> threading.Lock:
    key = str(run_id or "")
    if not key:
        # run_id が空のケースは想定外だが、落とさない
        return threading.Lock()
    with _COMPARE_RESTORE_LOCKS_GUARD:
        lock = _COMPARE_RESTORE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _COMPARE_RESTORE_LOCKS[key] = lock
        return lock


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
        # __enter__() を呼んだコンテキスト上の「元のスタック」を保持する。
        # ContextVar の Token は作成した Context と異なる Context では reset() できず ValueError になるため、
        # スレッド/タスク跨ぎ等で after_agent が別 Context になった場合のフォールバックに使う。
        self._prev_stack: Optional[list[tuple[str, str]]] = None
    
    def __enter__(self) -> "AgentInvocationContext":
        stack = _agent_stack.get().copy()
        self._prev_stack = stack.copy()
        if self._prefix:
            stack.extend(self._prefix)
        stack.append((self._invocation_id, self._agent_name))
        self._token = _agent_stack.set(stack)
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._token is not None:
            # 直前の状態へ確実に戻す（同一 Context なら reset が最も正しい）
            try:
                _agent_stack.reset(self._token)
            except ValueError:
                # Token が別 Context で作られている場合に発生:
                # ValueError: <Token ...> was created in a different Context
                #
                # ここで例外にすると run 全体が落ちるため、現在の Context 側で安全に復旧する。
                if self._prev_stack is not None:
                    _agent_stack.set(self._prev_stack)
                else:
                    _agent_stack.set([])
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

    # `compare_*` ツールを呼ぶ前に、必要なら COMPARE_STATE を復元するための設定
    compare_restore: Optional[CompareRestoreConfig] = None
    
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

        # ThreadLocal な COMPARE_STATE 対策:
        # - compare_setup は pipeline スレッドで実行されるが、subagent/tool 実行は別スレッドになり得る
        # - compare_* ツールはメモリ上の COMPARE_STATE が空だと即エラーになる
        # → compare_* 呼び出し直前に、同一runの成果物から compare_setup を“このスレッド”で再実行して復元する
        self._maybe_restore_compare_state(tool_name)

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

    def _maybe_restore_compare_state(self, tool_name: str) -> None:
        cfg = self.compare_restore
        if cfg is None:
            return

        tn = str(tool_name or "")
        if not tn.startswith("compare_") or tn == "compare_setup":
            return

        # read_ast は compare_state 非依存なので復元不要
        if tn == "read_ast":
            return

        try:
            from src.tools import COMPARE_STATE  # type: ignore
        except Exception:
            return

        # 既にこのスレッドで初期化済みなら何もしない
        if bool(COMPARE_STATE):
            return

        # 同一runの同時復元を抑制
        lock = _get_compare_restore_lock(self.run_id)
        with lock:
            # lock取得後に再チェック（他スレッドが先に復元した可能性）
            try:
                if bool(COMPARE_STATE):
                    return
            except Exception:
                # ThreadLocalDict が壊れている等のケースは復元を試す
                pass

            ast_a = Path(str(cfg.ast_a_path))
            ast_b = Path(str(cfg.ast_b_path))
            txt_a = Path(str(cfg.txt_a_path)) if cfg.txt_a_path else None
            txt_b = Path(str(cfg.txt_b_path)) if cfg.txt_b_path else None
            cache_path = Path(str(cfg.cache_path))
            initial_matching_path = Path(str(cfg.initial_matching_path)) if cfg.initial_matching_path else None

            if not ast_a.exists() or not ast_b.exists():
                # 成果物が無ければ復元不能（この場合はツール側のエラーに委ねる）
                return

            # compare_setup.invoke で ThreadLocal な COMPARE_STATE をこのスレッドで作る
            try:
                from src.tools import compare_all_chunk_similarity_matching, compare_setup  # type: ignore
            except Exception:
                return

            # できればイベントに残す（デバッグしやすくする）
            try:
                self.events.emit(
                    self.run_id,
                    "compare_state_restore",
                    {"ts": _utcnow_iso(), "status": "restoring", "trigger": "tool_call", "tool_name": tn},
                )
            except Exception:
                pass

            try:
                # cache_path は、ファイルがなくても compare_setup 側で作れる（ただしコスト増）
                # ここではディレクトリだけ確保しておく
                if cache_path.parent and not cache_path.parent.exists():
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass

            try:
                compare_setup.invoke(
                    {
                        "docA": str(ast_a),
                        "docB": str(ast_b),
                        "docA_txt": (str(txt_a) if txt_a and txt_a.exists() else None),
                        "docB_txt": (str(txt_b) if txt_b and txt_b.exists() else None),
                        "embedding_model": cfg.embedding_model,
                        "cache_path": str(cache_path),
                        "batch_size": int(cfg.embedding_batch_size),
                    }
                )

                # run成果物として initial_matching があれば、それを復元（which="last" のフォールバックにもなる）
                if initial_matching_path is not None and initial_matching_path.exists():
                    try:
                        loaded = json.loads(initial_matching_path.read_text(encoding="utf-8"))
                        COMPARE_STATE["initial_matching"] = loaded
                    except Exception:
                        pass

                # 必要なら warmup も行う（ファイルが無い/復元できない時の保険）
                if cfg.warmup_matching and not COMPARE_STATE.get("initial_matching"):
                    try:
                        warm = compare_all_chunk_similarity_matching.invoke(
                            {
                                "top_k": int(cfg.match_top_k),
                                "alpha": float(cfg.match_alpha),
                                "beta": float(cfg.match_beta),
                                "min_score": float(cfg.match_min_score),
                            }
                        )
                        try:
                            COMPARE_STATE["initial_matching"] = json.loads(warm)
                        except Exception:
                            pass
                    except Exception:
                        pass

                # 目印（別runとの混線回避とデバッグ用）
                try:
                    COMPARE_STATE["_run_id"] = self.run_id
                except Exception:
                    pass

                try:
                    self.events.emit(
                        self.run_id,
                        "compare_state_restore",
                        {"ts": _utcnow_iso(), "success": True, "trigger": "tool_call", "tool_name": tn},
                    )
                except Exception:
                    pass
            except Exception as e:
                try:
                    self.events.emit(
                        self.run_id,
                        "compare_state_restore",
                        {"ts": _utcnow_iso(), "success": False, "trigger": "tool_call", "tool_name": tn, "reason": str(e)},
                    )
                except Exception:
                    pass
                # ここで例外は上げない（元のtool実行でエラーになるなら、それに委ねる）
                return

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

