from langchain_openai import AzureChatOpenAI, ChatOpenAI
import os
import threading
import json
from pathlib import Path
from datetime import datetime
from typing import Any, Callable, Optional, Dict
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain.agents.middleware import AgentMiddleware,AgentState,ModelRequest,ModelResponse
from langchain.agents.middleware.types import ToolCallRequest

# ログディレクトリの定数
LOG_DIR = Path("log")

def build_llm(**kwargs):
    """環境変数から OpenAI / Azure OpenAI のチャットモデルを作成する。"""
    provider = (os.getenv("LLM_PROVIDER") or "openai").lower()
    temperature = float(os.getenv("TEMPERATURE") or "0")
    model = kwargs.pop("model", "gpt-5-mini")

    if provider in {"azure", "azureopenai", "azure_openai"}:
        return AzureChatOpenAI(
            azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
            azure_deployment=os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME")
            or os.getenv("AZURE_OPENAI_DEPLOYMENT")
            or model,
            api_version=os.getenv("AZURE_OPENAI_API_VERSION")
            or os.getenv("OPENAI_API_VERSION"),
            api_key=os.getenv("AZURE_OPENAI_API_KEY"),
            # temperature=temperature,
            **kwargs,
        )

    return ChatOpenAI(
        model=model,
        api_key=os.getenv("OPENAI_API_KEY"),
        # temperature=temperature,
        **kwargs,
    )

class DebugLoggingMiddleware(AgentMiddleware):
    """
    エージェントの各処理をJSONファイルにリアルタイムで出力するデバッグ用ミドルウェア。
    
    改善点:
    - グローバル状態管理により並列サブエージェントを正確に追跡
    - 親子関係の記録
    - taskツール呼び出しとサブエージェント起動の紐付け
    """
    
    # クラスレベルの共有状態（スレッドセーフ）
    _lock = threading.Lock()
    _agent_registry: Dict[str, dict] = {}  # invoke_id -> agent_info
    _pending_task_calls: Dict[str, str] = {}  # tool_call_id -> parent_invoke_id
    _file_initialized = False
    
    def __init__(
        self,
        log_file: str = "agent_debug.jsonl",
        overwrite: bool = False,
        include_full_messages: bool = False,
        is_subagent: bool = False,  # 新規: サブエージェントかどうかを明示
    ):
        """
        Args:
            log_file: 出力先のJSONLファイル名（logディレクトリ内に保存）
            overwrite: Trueの場合、最初のメインエージェント起動時にファイルを上書き
            include_full_messages: Trueの場合、メッセージ全体を出力
            is_subagent: Trueの場合、このミドルウェアはサブエージェント用
        """
        # logディレクトリ内にログファイルを保存
        self.log_file = LOG_DIR / log_file
        self.overwrite = overwrite
        self.include_full_messages = include_full_messages
        self.is_subagent = is_subagent
        self._step_counter = 0
        self._invoke_id = None
        self._parent_invoke_id = None
    
    def _write_log(self, log_entry: dict):
        """ログエントリをJSONLファイルに書き込む（スレッドセーフ）"""
        log_entry["timestamp"] = datetime.now().isoformat()
        log_entry["step"] = self._step_counter
        log_entry["invoke_id"] = self._invoke_id
        
        # 親子関係の情報を追加
        if self._parent_invoke_id:
            log_entry["parent_invoke_id"] = self._parent_invoke_id
        if self.is_subagent:
            log_entry["is_subagent"] = True
        
        self._step_counter += 1
        
        with self._lock:
            # logディレクトリが存在しない場合は作成
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            
            # 最初のメインエージェント起動時のみ上書き
            if self.overwrite and not DebugLoggingMiddleware._file_initialized:
                mode = "w"
                DebugLoggingMiddleware._file_initialized = True
            else:
                mode = "a"
            
            print(json.dumps(log_entry, ensure_ascii=False, default=str))
            with open(self.log_file, mode, encoding="utf-8") as f:
                f.write(json.dumps(log_entry, ensure_ascii=False, default=str) + "\n")
    
    def _summarize_messages(self, messages: list) -> list:
        """メッセージリストを要約形式に変換"""
        summary = []
        for m in messages:
            if isinstance(m, HumanMessage):
                content = m.content
                if len(content) > 200:
                    content = content[:200] + "..."
                summary.append({"type": "human", "content": content})
            elif isinstance(m, AIMessage):
                entry = {"type": "ai"}
                if m.content:
                    content = m.content
                    if len(content) > 200:
                        content = content[:200] + "..."
                    entry["content"] = content
                if m.tool_calls:
                    entry["tool_calls"] = [
                        {"name": tc.get("name"), "args_keys": list(tc.get("args", {}).keys())}
                        for tc in m.tool_calls
                    ]
                summary.append(entry)
            elif isinstance(m, ToolMessage):
                content = m.content if isinstance(m.content, str) else str(m.content)
                if len(content) > 200:
                    content = content[:200] + "..."
                summary.append({
                    "type": "tool_result",
                    "tool_call_id": m.tool_call_id,
                    "content": content,
                })
        return summary
    
    def _find_parent_from_pending_tasks(self) -> Optional[str]:
        """
        保留中のtaskツール呼び出しから親エージェントを特定する。
        最も最近のpending taskを親として採用する。
        """
        with self._lock:
            if not DebugLoggingMiddleware._pending_task_calls:
                return None
            
            # 最新のpending task（時間順でソートして最後のもの）
            # ここでは単純に最後に追加されたものを使用
            # 注: 実際にはタイムスタンプでソートするのが理想
            pending_ids = list(DebugLoggingMiddleware._pending_task_calls.values())
            return pending_ids[-1] if pending_ids else None
    
    def before_agent(self, state: AgentState, runtime) -> Optional[dict]:
        """エージェント開始時のログ"""
        self._step_counter = 0
        self._invoke_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        
        # サブエージェントの場合、親を特定
        if self.is_subagent:
            self._parent_invoke_id = self._find_parent_from_pending_tasks()
        
        # レジストリに登録
        with self._lock:
            DebugLoggingMiddleware._agent_registry[self._invoke_id] = {
                "is_subagent": self.is_subagent,
                "parent_invoke_id": self._parent_invoke_id,
                "start_time": datetime.now().isoformat(),
            }
        
        messages = state.get("messages", [])
        self._write_log({
            "event": "agent_start",
            "message_count": len(messages),
            "last_message": self._summarize_messages(messages[-1:]) if messages else [],
        })
        return None
    
    def before_model(self, state: AgentState, runtime) -> Optional[dict]:
        """モデル呼び出し前のログ（要約検知含む）"""
        messages = state.get("messages", [])
        
        # 要約メッセージの検知
        summary_detected = False
        summary_content = None
        for m in messages:
            if isinstance(m, HumanMessage) and "summary of the conversation" in (m.content or "").lower():
                summary_detected = True
                summary_content = m.content
                break
        
        log_entry = {
            "event": "before_model",
            "message_count": len(messages),
        }
        
        if summary_detected:
            log_entry["summarization_detected"] = True
            log_entry["summary_content"] = summary_content
        
        if self.include_full_messages:
            log_entry["messages"] = self._summarize_messages(messages)
        
        self._write_log(log_entry)
        return None
    
    def after_model(self, state: AgentState, runtime) -> Optional[dict]:
        """モデル応答後のログ（AIの思考を記録）"""
        messages = state.get("messages", [])
        
        # 最新のAIメッセージを取得
        ai_message = None
        for m in reversed(messages):
            if isinstance(m, AIMessage):
                ai_message = m
                break
        
        log_entry = {
            "event": "after_model",
        }
        
        if ai_message:
            # AIの思考内容
            if ai_message.content:
                log_entry["ai_thought"] = ai_message.content
            
            # ツール呼び出しの情報
            if ai_message.tool_calls:
                log_entry["tool_calls"] = [
                    {
                        "name": tc.get("name"),
                        "args": tc.get("args"),
                        "id": tc.get("id"),
                    }
                    for tc in ai_message.tool_calls
                ]
            
            # トークン使用量
            if hasattr(ai_message, "response_metadata") and ai_message.response_metadata:
                usage = ai_message.response_metadata.get("token_usage", {})
                if usage:
                    log_entry["token_usage"] = {
                        "prompt_tokens": usage.get("prompt_tokens"),
                        "completion_tokens": usage.get("completion_tokens"),
                        "total_tokens": usage.get("total_tokens"),
                    }
        
        self._write_log(log_entry)
        return None
    
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        """ツール呼び出しをラップしてログ出力"""
        tool_call = request.tool_call
        tool_name = tool_call.get("name", "unknown")
        tool_args = tool_call.get("args", {})
        tool_call_id = tool_call.get("id", "")
        
        # taskツールの場合、サブエージェント起動を追跡
        is_task_tool = tool_name == "task"
        if is_task_tool:
            with self._lock:
                DebugLoggingMiddleware._pending_task_calls[tool_call_id] = self._invoke_id
        
        # ツール呼び出し開始ログ
        self._write_log({
            "event": "tool_call_start",
            "tool_name": tool_name,
            "tool_args": tool_args,
            "tool_call_id": tool_call_id,
        })
        
        start_time = datetime.now()
        try:
            result = handler(request)
            duration_ms = (datetime.now() - start_time).total_seconds() * 1000
            
            # resultの型に応じて処理を分岐
            log_entry = {
                "event": "tool_call_result",
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "duration_ms": round(duration_ms, 2),
                "status": "success",
            }
            
            # ToolMessageの場合
            if hasattr(result, "content"):
                content = result.content if isinstance(result.content, str) else str(result.content)
                if len(content) > 1000:
                    log_entry["result_preview"] = content[:1000] + "..."
                    log_entry["result_length"] = len(content)
                else:
                    log_entry["result"] = content
            # Commandオブジェクトやその他の場合
            else:
                log_entry["result_type"] = type(result).__name__
                # Commandオブジェクトの場合、可能であれば詳細を取得
                if hasattr(result, "__dict__"):
                    log_entry["result_info"] = str(result)[:500]
            
            self._write_log(log_entry)
            return result
        
        except Exception as e:
            duration_ms = (datetime.now() - start_time).total_seconds() * 1000
            self._write_log({
                "event": "tool_call_error",
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "duration_ms": round(duration_ms, 2),
                "error": str(e),
                "error_type": type(e).__name__,
            })
            raise
        
        finally:
            # taskツールの場合、pending状態を解除
            if is_task_tool:
                with self._lock:
                    DebugLoggingMiddleware._pending_task_calls.pop(tool_call_id, None)
    
    def after_agent(self, state: AgentState, runtime) -> Optional[dict]:
        """エージェント終了時のログ"""
        messages = state.get("messages", [])
        
        # 最終的なAI応答を取得
        final_content = None
        for m in reversed(messages):
            if isinstance(m, AIMessage) and m.content and not m.tool_calls:
                final_content = m.content
                break
        
        self._write_log({
            "event": "agent_end",
            "total_messages": len(messages),
            "final_response_preview": final_content[:500] + "..." if final_content and len(final_content) > 500 else final_content,
        })
        
        # レジストリから削除
        with self._lock:
            DebugLoggingMiddleware._agent_registry.pop(self._invoke_id, None)
        
        if not self.is_subagent:
            print(f"📝 Debug log saved to: {self.log_file}")
        return None
    
    @classmethod
    def reset_global_state(cls):
        """
        グローバル状態をリセット（テスト用または新規実行開始時に使用）
        """
        with cls._lock:
            cls._agent_registry.clear()
            cls._pending_task_calls.clear()
            cls._file_initialized = False

def convert_pdf_to_txt(target_file):
    if target_file.endswith(".pdf"):
        # pymupdf (fitz)を使用して日本語PDFを正しく読み込む
        import fitz  # PyMuPDF
        doc = fitz.open(os.path.join("data", "input", target_file))
        text_content = []
        for page_num in range(len(doc)):
            page = doc[page_num]
            text_content.append(page.get_text())
        doc.close()

        target_file = target_file.replace(".pdf", ".txt")
        with open(os.path.join("data", "input", target_file), "w", encoding="utf-8") as f:
            f.write("\n".join(text_content))
        return
    return


def extract_message_logs(result: dict) -> list:
    """
    LangChainエージェントの実行結果からメッセージログを抽出する。
    
    引数:
        result: agent.invoke()の戻り値（messagesキーを含む辞書）
    
    戻り値:
        メッセージログのリスト。各ログは以下の形式:
        - human_message: {"type": "human_message", "index": int, "content": str}
        - ai_thought: {"type": "ai_thought", "index": int, "content": str}
        - tool_call: {"type": "tool_call", "index": int, "tool_name": str, "args": dict, 
                      "tool_call_id": str, "ai_content": str, "output": str, "status": str}
        - tool_result: {"type": "tool_result", "index": int, "tool_call_id": str, "status": str}
    """
    # tool_call_id -> ToolMessage
    _tool_messages = {
        m.tool_call_id: m for m in result["messages"] if isinstance(m, ToolMessage)
    }
    
    all_logs = []
    for idx, m in enumerate(result["messages"]):
        if isinstance(m, HumanMessage):
            all_logs.append({
                "type": "human_message",
                "index": idx,
                "content": m.content,
            })
        elif isinstance(m, AIMessage):
            # AIの思考プロセス（ツール呼び出しがない場合）
            if not m.tool_calls:
                all_logs.append({
                    "type": "ai_thought",
                    "index": idx,
                    "content": m.content,
                })
            # ツール呼び出しがある場合
            else:
                for tc in m.tool_calls:
                    tool_msg = _tool_messages.get(tc.get("id"))
                    all_logs.append({
                        "type": "tool_call",
                        "index": idx,
                        "tool_name": tc.get("name"),
                        "args": tc.get("args"),
                        "tool_call_id": tc.get("id"),
                        "ai_content": m.content,  # AIの思考プロセスも含める
                        "output": getattr(tool_msg, "content", None) if tool_msg else None,
                        "status": getattr(tool_msg, "status", None) if tool_msg else None,
                    })
        elif isinstance(m, ToolMessage):
            # ToolMessageは既にtool_callのログに含まれているので、必要に応じて追加
            all_logs.append({
                "type": "tool_result",
                "index": idx,
                "tool_call_id": m.tool_call_id,
                # "content": m.content,
                "status": getattr(m, "status", None),
            })
    
    return all_logs


def print_message_logs(logs: list):
    """
    メッセージログをJSON形式で出力する。
    
    引数:
        logs: extract_message_logs()の戻り値
    """
    for log in logs:
        print(json.dumps(log, ensure_ascii=False, indent=2))
        print("-" * 50)

def show_all_chunks_by_level(COMPARE_STATE):
    # 全チャンクのレベル（node_pathの長さ）を一覧表示
    if COMPARE_STATE:
        from collections import Counter
        
        chunks_a = COMPARE_STATE.get("chunks_a", [])
        chunks_b = COMPARE_STATE.get("chunks_b", [])
        
        def show_all_chunks_by_level(chunks, name):
            if not chunks:
                print(f"{name}: チャンクなし")
                return
            
            # レベル別に分類
            by_level = {}
            for chunk in chunks:
                level = len(chunk.node_path)
                if level not in by_level:
                    by_level[level] = []
                by_level[level].append(chunk)
            
            print(f"\n{'='*80}")
            print(f"=== {name} の全チャンク一覧（レベル別） ===")
            print(f"{'='*80}")
            print(f"総チャンク数: {len(chunks)}")
            
            # レベル別分布サマリー
            level_counts = Counter(len(c.node_path) for c in chunks)
            print(f"\nレベル別分布:")
            for level in sorted(level_counts.keys()):
                count = level_counts[level]
                pct = count / len(chunks) * 100
                print(f"  レベル{level}: {count}件 ({pct:.1f}%)")
            
            # 各チャンクの詳細
            print(f"\n{'─'*80}")
            print(f"{'No.':<4} {'Level':<6} {'文字数':>8} {'chunk_id':<25} {'title_path'}")
            print(f"{'─'*80}")
            
            for level in sorted(by_level.keys()):
                for chunk in by_level[level]:
                    content_len = len(chunk.content)
                    title_path_str = " > ".join(chunk.title_path[-3:])  # 最後の3階層のみ表示
                    if len(chunk.title_path) > 3:
                        title_path_str = "... > " + title_path_str
                    print(f"{chunks.index(chunk)+1:<4} L{level:<5} {content_len:>8,} {chunk.chunk_id:<25} {title_path_str[:50]}")
            print(f"{'─'*80}")
        
        show_all_chunks_by_level(chunks_a, "ドキュメントA")
        show_all_chunks_by_level(chunks_b, "ドキュメントB")
    else:
        print("COMPARE_STATEが初期化されていません。先にcompare_setupを実行してください。")

