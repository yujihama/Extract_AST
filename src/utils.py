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

def convert_pdf_to_txt(target_file: str, input_dir: str = "data/input") -> dict:
    """
    PDFをテキストに変換し、表・視覚的要素マーカーを統合して出力する。
    
    機能:
      - テキストストリーム順（PDF描画順序）でテキストを抽出
      - 表はMarkdown形式で挿入
      - 視覚要素（画像・グラフ・図）のマーカーをX+Y座標で適切な位置に挿入
    
    マーカー形式:
      <!-- VISUAL_ELEMENT page=N type=TYPE details=... -->
      - TYPE: image, chart, figure
    
    引数:
        target_file: PDFファイル名（例: "文書.pdf"）
        input_dir: 入力/出力ディレクトリ
    
    戻り値:
        dict: {
            output_path: str,  # 出力ファイルパス
            total_pages: int,
        }
    """
    if not target_file.endswith(".pdf"):
        return {"output_path": None, "visual_elements": [], "total_pages": 0}
    
    import fitz  # PyMuPDF
    import pdfplumber
    
    pdf_path = os.path.join(input_dir, target_file)
    doc = fitz.open(pdf_path)
    
    text_content = []
    
    try:
        with pdfplumber.open(pdf_path) as plumber_pdf:
            for page_num in range(len(doc)):
                fitz_page = doc[page_num]
                plumber_page = plumber_pdf.pages[page_num]
                page_width = fitz_page.rect.width
                
                # === 表を検出・抽出 ===
                tables = plumber_page.find_tables()
                table_bboxes = [table.bbox for table in tables]
                
                # 表のデータを抽出（挿入用）
                insertable_elements = []
                for table in tables:
                    data = table.extract()
                    if data:
                        insertable_elements.append({
                            "x0": table.bbox[0],
                            "x1": table.bbox[2],
                            "x_center": (table.bbox[0] + table.bbox[2]) / 2,
                            "y_top": table.bbox[1],
                            "text": _table_to_markdown(data),
                            "type": "table",
                        })
                
                # === 視覚要素を検出 ===
                # 1. 画像の検出（実際に表示されている画像のみ）
                image_info_list = fitz_page.get_image_info(xrefs=True)
                displayed_images = [
                    img for img in image_info_list
                    if img.get("bbox") and img["bbox"] != (0, 0, 0, 0)
                ]
                for idx, img in enumerate(displayed_images):
                    bbox = img["bbox"]
                    width = int(img.get("width", 0))
                    height = int(img.get("height", 0))
                    insertable_elements.append({
                        "x0": bbox[0],
                        "x1": bbox[2],
                        "x_center": (bbox[0] + bbox[2]) / 2,
                        "y_top": bbox[1],
                        "text": f"<!-- VISUAL_ELEMENT page={page_num + 1} type=image index={idx+1} size={width}x{height} -->",
                        "type": "visual",
                    })
                
                # 2. グラフ/チャートの検出 (色付き矩形が多い場合)
                colored_rects = [
                    r for r in (plumber_page.rects or [])
                    if r.get("fill") or r.get("non_stroking_color")
                ]
                if len(colored_rects) > 5:
                    chart_x0 = min(r.get("x0", 0) for r in colored_rects)
                    chart_x1 = max(r.get("x1", page_width) for r in colored_rects)
                    chart_y = min(r.get("top", 0) for r in colored_rects)
                    insertable_elements.append({
                        "x0": chart_x0,
                        "x1": chart_x1,
                        "x_center": (chart_x0 + chart_x1) / 2,
                        "y_top": chart_y,
                        "text": f"<!-- VISUAL_ELEMENT page={page_num + 1} type=chart rect_count={len(colored_rects)} -->",
                        "type": "visual",
                    })
                
                # 3. 曲線（グラフの線など）
                curves = plumber_page.curves or []
                if len(curves) > 10:
                    valid_curves = [c for c in curves if c.get("top") is not None]
                    if valid_curves:
                        figure_x0 = min(c.get("x0", 0) for c in valid_curves)
                        figure_x1 = max(c.get("x1", page_width) for c in valid_curves)
                        figure_y = min(c.get("top", 0) for c in valid_curves)
                        insertable_elements.append({
                            "x0": figure_x0,
                            "x1": figure_x1,
                            "x_center": (figure_x0 + figure_x1) / 2,
                            "y_top": figure_y,
                            "text": f"<!-- VISUAL_ELEMENT page={page_num + 1} type=figure curve_count={len(curves)} -->",
                            "type": "visual",
                        })
                
                # === テキストブロックを抽出 ===
                text_blocks = _extract_text_blocks_stream_order(fitz_page, table_bboxes)
                
                # === 2Dクラスタリングでソート（Y軸セクション→X軸カラム→Y座標順） ===
                text_blocks = _sort_blocks_2d_columns(text_blocks, x_gap=100, y_gap=30)
                
                # === 視覚要素・表をテキストブロックに挿入 ===
                # 各挿入要素について、適切な挿入位置を決定
                for elem in insertable_elements:
                    insert_idx = _find_insertion_index(elem, text_blocks, page_width)
                    text_blocks.insert(insert_idx, elem)
                
                # === テキストブロックを結合 ===
                output_parts = []
                current_paragraph = []
                
                for block in text_blocks:
                    if block["type"] in ("table", "visual"):
                        if current_paragraph:
                            output_parts.append("\n".join(current_paragraph))
                            current_paragraph = []
                        output_parts.append("\n" + block["text"] + "\n")
                    else:
                        current_paragraph.append(block["text"])
                
                if current_paragraph:
                    output_parts.append("\n".join(current_paragraph))
                
                page_text = "\n\n".join(output_parts)
                text_content.append(page_text)
    
    finally:
        doc.close()
    
    # テキストファイルに出力
    output_file = target_file.replace(".pdf", ".txt")
    output_path = os.path.join(input_dir, output_file)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(text_content))
    
    return {
        "output_path": output_path,
        "total_pages": len(text_content),
    }


def _extract_text_blocks_stream_order(fitz_page, table_bboxes: list) -> list:
    """
    PyMuPDFを使ってテキストブロックをテキストストリーム順（描画順序）で抽出する。
    表の領域内にあるブロックは除外する。
    
    Args:
        fitz_page: PyMuPDFのページオブジェクト
        table_bboxes: 表の領域リスト [(x0, y0, x1, y1), ...]
    
    Returns:
        list: テキストブロックのリスト（描画順序）
    """
    text_dict = fitz_page.get_text("dict")
    text_blocks = []
    
    for block in text_dict["blocks"]:
        if block["type"] != 0:  # テキストブロックのみ
            continue
        
        bbox = block["bbox"]
        block_center_x = (bbox[0] + bbox[2]) / 2
        block_center_y = (bbox[1] + bbox[3]) / 2
        
        # 表の領域内にあるブロックは除外
        in_table = False
        for table_bbox in table_bboxes:
            if _is_point_in_bbox(block_center_x, block_center_y, table_bbox):
                in_table = True
                break
        
        if in_table:
            continue
        
        # ブロック内のテキストを結合
        block_text_lines = []
        for line in block.get("lines", []):
            line_text = ""
            for span in line.get("spans", []):
                line_text += span.get("text", "")
            if line_text.strip():
                block_text_lines.append(line_text.strip())
        
        if block_text_lines:
            text_blocks.append({
                "x0": bbox[0],
                "x1": bbox[2],
                "x_center": block_center_x,
                "y_top": bbox[1],
                "y_bottom": bbox[3],
                "text": "\n".join(block_text_lines),
                "type": "text",
            })
    
    return text_blocks


def _find_insertion_index(element: dict, text_blocks: list, page_width: float) -> int:
    """
    視覚要素や表を挿入すべきインデックスを決定する。
    X座標（同じカラム内）とY座標の両方を考慮する。
    
    Args:
        element: 挿入する要素 {"x_center", "y_top", ...}
        text_blocks: テキストブロックのリスト
        page_width: ページ幅
    
    Returns:
        int: 挿入すべきインデックス
    """
    if not text_blocks:
        return 0
    
    elem_x = element["x_center"]
    elem_y = element["y_top"]
    
    # X座標の許容範囲（ページ幅の30%以内を「同じカラム」とみなす）
    x_tolerance = page_width * 0.3
    
    # 同じカラム内でY座標が要素より上にあるブロックを探す
    candidates = []
    for i, block in enumerate(text_blocks):
        if block["type"] != "text":
            continue
        
        block_x = block["x_center"]
        block_y_bottom = block.get("y_bottom", block["y_top"])
        
        # X座標が近い（同じカラム内）かつ Y座標が要素より上
        if abs(block_x - elem_x) < x_tolerance and block_y_bottom <= elem_y:
            candidates.append((i, block))
    
    if candidates:
        # 最も下にあるブロック（Y座標が最大）の後に挿入
        best_idx = max(candidates, key=lambda x: x[1].get("y_bottom", x[1]["y_top"]))[0]
        return best_idx + 1
    
    # 同じカラム内に候補がない場合、Y座標のみで判定
    for i, block in enumerate(text_blocks):
        if block["y_top"] > elem_y:
            return i
    
    # すべてのブロックより下にある場合は末尾に挿入
    return len(text_blocks)


def _detect_column_centers(blocks: list, min_gap: float = 100) -> list:
    """
    ブロックのX座標からカラム中心を検出する。
    隣接するX座標の大きなギャップでカラム境界を判定。
    
    Args:
        blocks: テキストブロックのリスト
        min_gap: カラム間の最小ギャップ（これより離れていたら別カラム）
    
    Returns:
        list: カラム中心のX座標リスト（左から右へソート済み）
    """
    if not blocks:
        return []
    
    # 全ブロックのX座標（中心）を収集してソート
    x_coords = sorted([b["x_center"] for b in blocks])
    
    if len(x_coords) == 1:
        return [x_coords[0]]
    
    # 隣接するX座標のギャップを計算し、大きなギャップでカラム境界を検出
    clusters = [[x_coords[0]]]
    
    for i in range(1, len(x_coords)):
        gap = x_coords[i] - x_coords[i - 1]
        if gap >= min_gap:
            # 大きなギャップがあれば新しいカラム
            clusters.append([x_coords[i]])
        else:
            # 隣接しているので同じカラム
            clusters[-1].append(x_coords[i])
    
    # 各クラスタの中心（平均）を計算
    column_centers = [sum(cluster) / len(cluster) for cluster in clusters]
    
    return column_centers


def _assign_blocks_to_columns(blocks: list, column_centers: list) -> dict:
    """
    各ブロックを最も近いカラムに割り当てる。
    
    Args:
        blocks: テキストブロックのリスト
        column_centers: カラム中心のX座標リスト
    
    Returns:
        dict: {カラムインデックス: [ブロックリスト]}
    """
    columns = {i: [] for i in range(len(column_centers))}
    
    for block in blocks:
        # 最も近いカラム中心を見つける
        min_dist = float('inf')
        closest_col = 0
        for i, center in enumerate(column_centers):
            dist = abs(block["x_center"] - center)
            if dist < min_dist:
                min_dist = dist
                closest_col = i
        
        columns[closest_col].append(block)
    
    return columns


def _sort_blocks_2d_columns(blocks: list, x_gap: float = 50, y_gap: float = 30) -> list:
    """
    X軸とY軸の両方でクラスタリングしてブロックをソートする。
    
    読み順:
    - Y軸セクション順（上→下）
    - 各セクション内でX軸カラム順（左→右）
    - 各カラム内でY座標順（上→下）
    
    Args:
        blocks: テキストブロックのリスト
        x_gap: X軸のカラム間最小ギャップ
        y_gap: Y軸のセクション間最小ギャップ
    
    Returns:
        list: ソート済みブロックリスト
    """
    if not blocks:
        return []
    
    # ステップ1: Y軸でセクション分割
    sorted_by_y = sorted(blocks, key=lambda b: b["y_top"])
    
    y_sections = [[sorted_by_y[0]]]
    for block in sorted_by_y[1:]:
        prev_bottom = y_sections[-1][-1].get("y_bottom", y_sections[-1][-1]["y_top"])
        if block["y_top"] - prev_bottom > y_gap:
            y_sections.append([block])
        else:
            y_sections[-1].append(block)
    
    # ステップ2: 各セクション内でX軸カラム検出・ソート
    result = []
    for section in y_sections:
        # X座標でカラム中心を検出
        column_centers = _detect_column_centers(section, x_gap)
        
        if not column_centers:
            # カラムが検出できない場合はY座標順
            result.extend(sorted(section, key=lambda b: b["y_top"]))
            continue
        
        # ブロックをカラムに割り当て
        columns = _assign_blocks_to_columns(section, column_centers)
        
        # カラム順（左→右）、各カラム内でY座標順（上→下）
        for col_idx in range(len(column_centers)):
            col_blocks = columns[col_idx]
            col_sorted = sorted(col_blocks, key=lambda b: b["y_top"])
            result.extend(col_sorted)
    
    return result


def _is_point_in_bbox(x: float, y: float, bbox: tuple) -> bool:
    """点が矩形内にあるかチェック"""
    x0, y0, x1, y1 = bbox
    return x0 < x < x1 and y0 < y < y1


def _table_to_markdown(table_data: list) -> str:
    """表データをMarkdown形式に変換する"""
    if not table_data or not table_data[0]:
        return ""
    
    def clean_cell(cell):
        if cell is None:
            return ""
        text = str(cell).strip().replace("\n", " ").replace("|", "\\|")
        return text
    
    lines = []
    header = table_data[0]
    
    # ヘッダー行
    header_cells = [clean_cell(cell) for cell in header]
    lines.append("| " + " | ".join(header_cells) + " |")
    
    # 区切り行
    lines.append("| " + " | ".join(["---"] * len(header)) + " |")
    
    # データ行
    for row in table_data[1:]:
        row_cells = []
        for i, cell in enumerate(row):
            if i < len(header):
                row_cells.append(clean_cell(cell))
        # 不足している列を補完
        while len(row_cells) < len(header):
            row_cells.append("")
        lines.append("| " + " | ".join(row_cells) + " |")
    
    return "\n".join(lines)


def _chars_to_text_blocks(chars: list) -> list:
    """文字リストをテキストブロックに変換する"""
    if not chars:
        return []
    
    lines_dict = {}
    for char in chars:
        y_key = round(char["top"] / 5) * 5
        if y_key not in lines_dict:
            lines_dict[y_key] = []
        lines_dict[y_key].append(char)
    
    text_blocks = []
    for y_key in sorted(lines_dict.keys()):
        line_chars = sorted(lines_dict[y_key], key=lambda c: c["x0"])
        line_text = "".join(c["text"] for c in line_chars).strip()
        if line_text:
            text_blocks.append({
                "y_top": y_key,
                "text": line_text,
                "type": "text",
            })
    
    return text_blocks


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

