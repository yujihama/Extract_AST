from langchain_core.tools import tool
import os
import re
from typing import List, Optional
from pydantic import BaseModel, Field

@tool
def read_text_segment(file_path: str, start: int, length: int, intent: str) -> str:
    """
    テキストファイルの特定のセグメントを読み取る。

    引数:
        file_path: テキストファイルのパス。
        start: 開始文字インデックス（0ベース）。
        length: 読み取る文字数。
        intent: ツール呼び出しの意図。

    戻り値:
        'start' から指定された 'length' の部分文字列。
    """
    try:
        # file_pathが / で始まる場合も許容する（相対・絶対どちらもOK）
        clean_path = file_path.lstrip("/")
        normalized_path = os.path.normpath(clean_path)
        with open(normalized_path, 'r', encoding='utf-8') as f:
            if start > 0:
                f.read(start)
            return f.read(length)
    except Exception as e:
        return f"Error reading file: {str(e)}"

@tool
def extract_regex_matches(
    file_path: str,
    regex_pattern: str,
    intent: str,
    regex_patterns: Optional[List[str]] = None,
    max_matches: int = 50,
    offset_matches: int = 0,
    max_output_chars: int = 8000,
    count_only: bool = False,
    dedupe: bool = False,
    save_to: Optional[str] = None,
    include_line_text: bool = True,
) -> str:
    """
    ファイルから指定された正規表現パターンに一致するテキストを抽出する。

    - `regex_patterns` を指定すると、複数正規表現で検索して「行番号＋行テキスト＋マッチ文字列」を返す。
    - `regex_patterns` が未指定の場合は、従来どおり `regex_pattern`（単一）で検索する。

    引数:
        file_path: テキストファイルのパス。
        regex_pattern: 検索する Python スタイルの正規表現パターン（後方互換用。`regex_patterns` 指定時は無視される）。
        intent: ツール呼び出しの意図。
        regex_patterns: 検索する正規表現パターンのリスト（複数指定用）。
        max_matches: 返却する一致（サンプル）の最大件数（デフォルト: 50）。
        offset_matches: 先頭からスキップする一致件数（ページング用途）。
        max_output_chars: 返却文字列の上限（ベストエフォート）。超える場合はサンプルを削って短縮する。
        count_only: True の場合、件数のみ返す（サンプルは返さない）。
        dedupe: True の場合、サンプルを重複排除して返す（返却件数は max_matches まで）。
        save_to: 指定した場合、offset_matches 以降の一致を JSONL でファイルへ保存する（コンテキスト節約用）。
        include_line_text: True の場合、ヒットした行テキストも返す。

    戻り値:
        JSON 文字列（件数、サンプル、トランケーション有無、保存先など）。

    返却される `sample` の要素は以下の形:
        {
          "pattern_index": 0,
          "pattern": "...",
          "line_number": 123,
          "match": "..." | ["...", ...],
          "full_match": "...",
          "line": "..."  # include_line_text=true の場合
        }

    注意:
        このツールは「行番号」を返すため、検索は基本的に行単位で行う（複数行にまたがる正規表現は想定外）。

    **`HierarchyRule.validation_rules` との連携:**
        このツールで取得した `line_number` 情報を使って、`HierarchyRule` の `validation_rules` で定義された条件を適用できます。
        以下のような条件フィルタリングが可能です（Workerエージェント側で実装が必要）:

        1. **位置スコープ (`min_line`, `max_line`)**:
           - 例: `min_line: 200` → 200行目以降のみ候補として扱う
           - 用途: 目次やヘッダーを除外し、本文のみを抽出する
           - 実装: `line_number >= validation_rules.min_line` かつ `line_number <= validation_rules.max_line` をチェック

        2. **近傍行条件 (`prev_line_regex`, `next_line_regex`)**:
           - 例: `prev_line_regex: '^$'` → 直前の行が空行であることを必須とする
           - 例: `next_line_regex: '^\\s{2,}\\S'` → 次行がインデントされていることを必須とする
           - 用途: 見出しと本文中の箇条書きを区別する
           - 実装: `read_text_file` で該当行の前後を読み取り、正規表現でマッチするかチェック

        3. **アンカー条件 (`only_after_first_match_of`)**:
           - 例: `only_after_first_match_of: 'level:1'` → Level 1 の見出しが最初に出現した後のみ有効
           - 例: `only_after_first_match_of: 'name:Major_Section'` → 特定の名前の見出しが出現した後のみ有効
           - 用途: 目次ブロック内の見出しパターンを本文から除外する
           - 実装: 先にアンカー見出しの `line_number` を記録し、それより後の行のみ候補とする

        検証フェーズでは、`extract_regex_matches` で取得した候補に対して、これらの条件を適用して誤検知を除外してください。
    """
    try:
        import json

        if max_matches < 0:
            max_matches = 0
        if offset_matches < 0:
            offset_matches = 0
        if max_output_chars is None or max_output_chars <= 0:
            max_output_chars = 8000

        patterns_in = regex_patterns if (regex_patterns is not None and len(regex_patterns) > 0) else [regex_pattern]
        # 空文字は除外（`regex_patterns` を使う場合に `regex_pattern` をダミーで渡してもOKにする）
        patterns_in = [p for p in patterns_in if isinstance(p, str) and p.strip()]
        if not patterns_in:
            return json.dumps(
                {
                    "ok": False,
                    "error": "regex_pattern または regex_patterns に少なくとも1つの正規表現を指定してください。",
                },
                ensure_ascii=False,
            )

        compiled = []
        for i, p in enumerate(patterns_in):
            try:
                compiled.append((i, p, re.compile(p)))
            except Exception as e:
                return json.dumps(
                    {
                        "ok": False,
                        "error": f"正規表現のコンパイルに失敗しました (index={i}): {str(e)}",
                        "pattern": p,
                    },
                    ensure_ascii=False,
                )

        def _match_to_item(m: re.Match, group_count: int):
            # re.findall と同等の返却形（グループなし: 全体、グループ1つ: そのグループ、複数: タプル）
            if group_count == 0:
                return m.group(0)
            if group_count == 1:
                return m.group(1)
            return m.groups()

        # save_to が指定されている場合は JSONL で全件（offset 以降）を保存
        save_f = None
        if save_to:
            save_dir = os.path.dirname(os.path.abspath(save_to))
            if save_dir:
                os.makedirs(save_dir, exist_ok=True)
            save_f = open(save_to, "w", encoding="utf-8", newline="\n")

        total = 0
        total_after_offset = 0
        sample = []
        seen = set()

        clean_path = file_path.lstrip("/")
        normalized_path = os.path.normpath(clean_path)
        try:
            with open(normalized_path, "r", encoding="utf-8") as f:
                for line_number, line in enumerate(f, start=1):
                    line_no_nl = line.rstrip("\n")
                    for pattern_index, pattern_text, pattern_obj in compiled:
                        group_count = pattern_obj.groups
                        for m in pattern_obj.finditer(line_no_nl):
                            total += 1
                            if total <= offset_matches:
                                continue
                            total_after_offset += 1

                            item = _match_to_item(m, group_count)

                            record = {
                                "pattern_index": pattern_index,
                                "pattern": pattern_text,
                                "line_number": line_number,
                                "match": (list(item) if isinstance(item, tuple) else item),
                                "full_match": m.group(0),
                            }
                            if include_line_text:
                                record["line"] = line_no_nl

                            if save_f is not None:
                                save_f.write(json.dumps(record, ensure_ascii=False) + "\n")

                            if count_only:
                                continue

                            if len(sample) >= max_matches:
                                continue

                            if dedupe:
                                key = repr(record)
                                if key in seen:
                                    continue
                                seen.add(key)

                            sample.append(record)
        finally:
            if save_f is not None:
                save_f.close()

        payload = {
            "ok": True,
            "file_path": file_path,
            "regex_pattern": regex_pattern,
            "regex_patterns": patterns_in,
            "total_matches": total,
            "offset_matches": offset_matches,
            "matches_after_offset": total_after_offset,
            "returned_matches": 0 if count_only else len(sample),
            "truncated": (False if count_only else (total_after_offset > len(sample))),
            "count_only": bool(count_only),
            "dedupe": bool(dedupe),
            "saved_to": save_to,
            "include_line_text": bool(include_line_text),
            "sample": [] if count_only else sample,
            "hint": "結果が多い場合は count_only=true で件数確認→ offset_matches でページング。全件が必要なら save_to を指定してください。",
        }

        # 出力サイズが大きい場合は sample を削って短縮（ベストエフォート）
        def _dump(p: dict) -> str:
            return json.dumps(p, ensure_ascii=False)

        out = _dump(payload)
        if not count_only and len(out) > max_output_chars:
            while payload["sample"] and len(out) > max_output_chars:
                payload["sample"].pop()
                payload["returned_matches"] = len(payload["sample"])
                payload["truncated"] = True
                out = _dump(payload)
            if len(out) > max_output_chars:
                payload["sample"] = []
                payload["returned_matches"] = 0
                payload["truncated"] = True
                payload["hint"] = (
                    "出力が大きすぎるためサンプルは省略しました。count_only=true で件数確認し、"
                    "必要なら save_to でファイル出力してください。"
                )
                out = _dump(payload)

        return out
    except Exception as e:
        return f"Error extracting matches: {str(e)}"

@tool
def get_file_length(file_path: str, intent: str) -> str:
    """
    ファイルの総文字数を返す。
    
    引数:
        file_path: テキストファイルのパス。
        intent: ツール呼び出しの意図。

    戻り値:
        ファイルの総文字数。
    """
    try:
        clean_path = file_path.lstrip("/")
        normalized_path = os.path.normpath(clean_path)

        with open(normalized_path, 'r', encoding='utf-8') as f:
            return str(len(f.read()))
    except Exception as e:
        return f"Error getting file length: {str(e)}"

class ReadTextFileArgs(BaseModel):
    """read_text_file ツールの引数。"""
    file_path: str = Field(..., description="テキストファイルのパス。")
    start: Optional[int] = Field(None, description="開始文字インデックス（0ベース）。このパラメータを省略すると、先頭から読み取る。")
    length: Optional[int] = Field(None, description="読み取る文字数。このパラメータを省略すると、start 位置から 100 文字読み取る（start も省略されている場合は先頭から 100 文字）。")
    intent: str = Field(..., description="ツール呼び出しの意図。")

@tool(args_schema=ReadTextFileArgs)
def read_text_file(file_path: str, start: Optional[int] = None, length: Optional[int] = None, intent: str = "") -> str:
    """
    UTF-8 としてテキストファイルを読み取る。ファイルの特定のセグメントを読み取ることができる。
    
    デフォルト動作: start と length の両方を省略した場合、最初の 100 文字を読み取る。
    これにより、大きなファイルを読み取る際にコンテキストウィンドウの制限を超えることを防ぐ。
    
    例:
    - read_text_file("file.txt") -> 最初の 100 文字を読み取る
    - read_text_file("file.txt", start=0, length=1000) -> 最初の 1000 文字を読み取る
    - read_text_file("file.txt", start=500, length=2000) -> 位置 500 から 2000 文字を読み取る
    - read_text_file("file.txt", start=1000) -> 位置 1000 から 100 文字を読み取る
    
    引数:
        file_path: テキストファイルのパス。
        start: 開始文字インデックス（0ベース）。省略すると先頭から読み取る。
        length: 読み取る文字数。省略すると start 位置から 100 文字を読み取る。
        intent: ツール呼び出しの意図。
    戻り値:
        ファイルコンテンツのセグメント（デフォルト: start と length の両方を省略した場合は最初の 100 文字）。
    """
    try:
        # 注意: "start" は文字インデックスであり、バイトオフセットではない。
        # テキストモードでは、seek(start) を使用すると UTF-8 のマルチバイトシーケンスの途中に着地する可能性がある。

        clean_path = file_path.lstrip("/")
        normalized_path = os.path.normpath(clean_path)

        with open(normalized_path, 'r', encoding='utf-8') as f:
            if start is not None and start > 0:
                f.read(start)
            if length is not None:
                return f.read(length)
            # デフォルト: 100 文字を読み取る
            return f.read(100)
    except Exception as e:
        return f"Error reading file: {str(e)}"

# -*- coding: utf-8 -*-
"""
read_ast ツール - AST JSONファイルの効率的な読み込み

コンテキスト長を節約しながら、AST構造を段階的に読み込むためのツール。
"""

import json
import os
from typing import Any, Dict, List, Literal, Optional

from langchain_core.tools import tool


def _iter_nodes(root: Dict[str, Any], path: Optional[List[int]] = None):
    """Yield (node, path) preorder."""
    if path is None:
        path = []
    yield root, path
    children = root.get("children") or []
    if isinstance(children, list):
        for i, ch in enumerate(children):
            if isinstance(ch, dict):
                yield from _iter_nodes(ch, path + [i])


def _is_leaf(node: Dict[str, Any]) -> bool:
    children = node.get("children") or []
    return not (isinstance(children, list) and any(isinstance(c, dict) for c in children))


def _get_node_by_path(root: Dict[str, Any], node_path: List[int]) -> Dict[str, Any]:
    cur = root
    for idx in node_path:
        children = cur.get("children") or []
        if not isinstance(children, list) or idx >= len(children):
            raise KeyError(f"Invalid path {node_path}")
        cur = children[idx]
    return cur


def _build_title_path(root: Dict[str, Any], node_path: List[int]) -> List[str]:
    """ノードパスからタイトルパス（セクションタイトルのリスト）を構築"""
    titles = [root.get("section_title", "root")]
    cur = root
    for idx in node_path:
        children = cur.get("children") or []
        if isinstance(children, list) and idx < len(children):
            cur = children[idx]
            titles.append(cur.get("section_title", f"[{idx}]"))
    return titles


@tool
def read_ast(
    file_path: str,
    mode: Literal["summary", "outline", "chunk", "search"] = "summary",
    node_path: Optional[List[int]] = None,
    title_query: Optional[str] = None,
    max_depth: int = 3,
    include_content: bool = False,
    max_content_chars: int = 500,
) -> str:
    """
    AST JSONファイルを効率的に読み込むツール。

    モード:
        - "summary": ファイルメタデータと統計情報のみ（デフォルト、最小コンテキスト）
        - "outline": セクション構造をツリー形式で取得（max_depthで深さ制限）
        - "chunk": 特定のセクション（node_pathで指定）の詳細を取得
        - "search": タイトルで検索（title_queryで部分一致検索）

    引数:
        file_path: AST JSONファイルのパス
        mode: 読み込みモード
        node_path: (mode="chunk"時) セクションへのパス [0, 1, 2] 形式
        title_query: (mode="search"時) 検索するタイトルの部分文字列
        max_depth: (mode="outline"時) 表示する最大深さ（デフォルト3）
        include_content: アウトラインにcontent_summaryを含めるか
        max_content_chars: contentを切り詰める最大文字数

    戻り値:
        JSON形式の結果
    """
    try:
        # file_pathが / で始まる場合も許容する （相対・絶対どちらもOK）
        clean_path = file_path.lstrip("/")
        normalized_path = os.path.normpath(clean_path)
        # ファイル存在チェック
        if not os.path.exists(normalized_path):
            return json.dumps({
                "ok": False,
                "error": f"File not found: {normalized_path}"
            }, ensure_ascii=False)

        # ファイルサイズチェック（警告用）
        file_size = os.path.getsize(normalized_path)
        
        with open(normalized_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        root = data.get("root", {})
        meta = data.get("__meta__", {})
        file_name = data.get("file_name", os.path.basename(normalized_path))

        # === mode: summary ===
        if mode == "summary":
            # 統計情報を収集
            total_nodes = 0
            leaf_nodes = 0
            total_content_chars = 0
            max_depth_found = 0
            level_counts: Dict[int, int] = {}

            for node, path in _iter_nodes(root):
                total_nodes += 1
                depth = len(path)
                level_counts[depth] = level_counts.get(depth, 0) + 1
                max_depth_found = max(max_depth_found, depth)
                if _is_leaf(node):
                    leaf_nodes += 1
                content = node.get("content", "")
                if isinstance(content, str):
                    total_content_chars += len(content)

            return json.dumps({
                "ok": True,
                "mode": "summary",
                "file_path": file_path,
                "file_name": file_name,
                "file_size_bytes": file_size,
                "meta": meta,
                "root_title": root.get("section_title", ""),
                "root_summary": root.get("content_summary", "")[:500] if root.get("content_summary") else None,
                "stats": {
                    "total_sections": total_nodes,
                    "leaf_sections": leaf_nodes,
                    "max_depth": max_depth_found,
                    "total_content_chars": total_content_chars,
                    "estimated_tokens": total_content_chars // 2,
                    "sections_by_depth": level_counts,
                },
                "top_level_sections": [
                    {"index": i, "title": ch.get("section_title", "")}
                    for i, ch in enumerate(root.get("children", [])[:10])
                    if isinstance(ch, dict)
                ],
            }, ensure_ascii=False, indent=2)

        # === mode: outline ===
        elif mode == "outline":
            outline = []
            for node, path in _iter_nodes(root):
                depth = len(path)
                if depth > max_depth:
                    continue
                
                entry = {
                    "depth": depth,
                    "node_path": path,
                    "title": node.get("section_title", ""),
                    "is_leaf": _is_leaf(node),
                    "children_count": len(node.get("children", [])),
                }
                
                if include_content:
                    summary = node.get("content_summary", "")
                    if summary:
                        entry["summary"] = summary[:200] + ("..." if len(summary) > 200 else "")
                
                outline.append(entry)

            return json.dumps({
                "ok": True,
                "mode": "outline",
                "file_path": file_path,
                "max_depth": max_depth,
                "total_shown": len(outline),
                "sections": outline,
            }, ensure_ascii=False, indent=2)

        # === mode: chunk ===
        elif mode == "chunk":
            if node_path is None:
                node_path = []
            
            try:
                if len(node_path) == 0:
                    target_node = root
                else:
                    target_node = _get_node_by_path(root, node_path)
            except KeyError as e:
                return json.dumps({
                    "ok": False,
                    "error": str(e),
                    "hint": "Use mode='outline' to see available paths"
                }, ensure_ascii=False)

            title_path = _build_title_path(root, node_path)
            content = target_node.get("content", "")
            
            # コンテンツが長すぎる場合は切り詰め
            content_truncated = False
            if isinstance(content, str) and len(content) > max_content_chars:
                content = content[:max_content_chars] + f"\n... (truncated, total {len(target_node.get('content', ''))} chars)"
                content_truncated = True

            children_info = []
            for i, ch in enumerate(target_node.get("children", [])[:20]):
                if isinstance(ch, dict):
                    children_info.append({
                        "index": i,
                        "title": ch.get("section_title", ""),
                        "has_content": bool(ch.get("content")),
                        "is_leaf": _is_leaf(ch),
                    })

            return json.dumps({
                "ok": True,
                "mode": "chunk",
                "node_path": node_path,
                "title_path": title_path,
                "section_title": target_node.get("section_title", ""),
                "content_summary": target_node.get("content_summary", ""),
                "content": content,
                "content_truncated": content_truncated,
                "is_leaf": _is_leaf(target_node),
                "children": children_info,
            }, ensure_ascii=False, indent=2)

        # === mode: search ===
        elif mode == "search":
            if not title_query:
                return json.dumps({
                    "ok": False,
                    "error": "title_query is required for search mode"
                }, ensure_ascii=False)

            query_lower = title_query.lower()
            results = []

            for node, path in _iter_nodes(root):
                title = node.get("section_title", "")
                if query_lower in title.lower():
                    results.append({
                        "node_path": path,
                        "title_path": _build_title_path(root, path),
                        "title": title,
                        "depth": len(path),
                        "is_leaf": _is_leaf(node),
                        "has_content": bool(node.get("content")),
                        "summary_preview": (node.get("content_summary", "") or "")[:100],
                    })

            return json.dumps({
                "ok": True,
                "mode": "search",
                "query": title_query,
                "total_matches": len(results),
                "results": results[:50],  # 最大50件
            }, ensure_ascii=False, indent=2)

        else:
            return json.dumps({
                "ok": False,
                "error": f"Unknown mode: {mode}",
                "valid_modes": ["summary", "outline", "chunk", "search"]
            }, ensure_ascii=False)

    except json.JSONDecodeError as e:
        return json.dumps({
            "ok": False,
            "error": f"Invalid JSON: {str(e)}"
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({
            "ok": False,
            "error": f"Error: {type(e).__name__}: {str(e)}"
        }, ensure_ascii=False)

# --- Compare Tools (LangChain Tool wrappers) ---
import json
import os
import importlib
from typing import List, Optional

from . import ast_compare
from pydantic import BaseModel, Field
from langchain_core.tools import tool

from .ast_compare import (
    HybridChunkIndex,
    compare_chunks,
    extract_leaf_chunks,
    load_ast,
    match_all_chunks,
    extract_chunks,
)

# ツール間で共有するグローバル状態
COMPARE_STATE: dict = {}

@tool
def compare_setup(docA: str, docB: str, embedding_model: Optional[str] = None, cache_path: str = "data/embedding/embedding_cache.json", batch_size: int = 128) -> str:
    """
    【必須】比較の初期化ツール（docA/docBを読み込み、チャンク化・Embedding Index作成を行います）。

    実行内容:
    - `docA` / `docB` の `*.ast.json` を読み込み
    - 最下層（leaf）ノードの `content` をチャンク化（chunk_id/title_path/node_path を付与）
    - OpenAI / Azure OpenAI の Embedding（text-embedding-3-large）でベクトル化し、検索用Indexを作成
    - 生成物を Notebook グローバル `COMPARE_STATE` に保存（以降のツールが参照）

    前提:
    - 先にこのセルを実行してツール定義済みであること
    - Embedding用の環境変数が設定済みであること
      - OpenAI: `OPENAI_API_KEY`（任意: `OPENAI_EMBEDDING_MODEL`）
      - Azure: `LLM_PROVIDER=azure`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_API_VERSION`
        （任意: `AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME`）

    引数:
    - docA: str = Field(..., description="docA の *.ast.json パス")
    - docB: str = Field(..., description="docB の *.ast.json パス")
    - embedding_model: Optional[str] = Field(
        default=None,
        description="Embeddingモデル/Deployment名。省略時は env に従う（OpenAI: OPENAI_EMBEDDING_MODEL / Azure: AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME）",
    )
    - cache_path: str = Field("data/embedding/embedding_cache.json", description="埋め込みキャッシュの保存先")
    - batch_size: int = Field(64, description="埋め込み取得のバッチサイズ")

    戻り値:
    - JSON文字列: { ok, docA, docB, docA_chunks, docB_chunks, cache_path }

    Tips:
    - `cache_path` を指定すると再実行時のEmbeddingコストを大きく減らせます。
    """
    ast_a = load_ast(docA)
    ast_b = load_ast(docB)

    doc_id_a = os.path.basename(docA)
    doc_id_b = os.path.basename(docB)

    chunks_a = extract_chunks(
                    ast=ast_a,
                    doc_id="doc_id_a",
                    strategy="level",
                    target_level=2,
                    min_chars_for_split=3000,
                )
    chunks_b = extract_chunks(
                    ast=ast_b,
                    doc_id="doc_id_b",
                    strategy="level",
                    target_level=2,
                    min_chars_for_split=3000,
                )

    index_a = HybridChunkIndex(chunks=chunks_a, embedding_model=embedding_model, cache_path=cache_path, batch_size=batch_size)
    index_b = HybridChunkIndex(chunks=chunks_b, embedding_model=embedding_model, cache_path=cache_path, batch_size=batch_size)

    COMPARE_STATE.clear()
    COMPARE_STATE.update(
        {
            "docA": docA,
            "docB": docB,
            "doc_id_a": doc_id_a,
            "doc_id_b": doc_id_b,
            "ast_a": ast_a,
            "ast_b": ast_b,
            "chunks_a": chunks_a,
            "chunks_b": chunks_b,
            "chunks_a_by_id": {c.chunk_id: c for c in chunks_a},
            "chunks_b_by_id": {c.chunk_id: c for c in chunks_b},
            "index_a": index_a,
            "index_b": index_b,
        }
    )

    return json.dumps(
        {
            "ok": True,
            "docA": docA,
            "docB": docB,
            "docA_chunks": len(chunks_a),
            "docB_chunks": len(chunks_b),
            "cache_path": cache_path,
        },
        ensure_ascii=False,
        indent=2,
    )

@tool
def compare_all_chunk_similarity_matching(top_k: int = 3, alpha: float = 0.3, beta: float = 0.4, min_score: float = 0.25) -> str:
    """
    docA の各チャンクを起点に、docB 側の対応候補（上位 top_k）を列挙してグルーピングします。

    返すデータは「どのAチャンクが、Bのどのチャンクに近いか」の候補集合です。
    
    スコア:
    - score = alpha * cosine + beta * title_keyword + (1 - alpha - beta) * content_keyword

    引数:
    - top_k: int = Field(3, description="各Aチャンクに対するB側の候補数")
    - alpha: float = Field(0.5, description="コサイン類似度（意味的類似性）の重み")
    - beta: float = Field(0.3, description="title_path類似度（セクション構造一致）の重み")
    - min_score: float = Field(0.25, description="このスコア未満はマッチ候補から除外")

    出力(JSON文字列):
    - ok: bool
    - groups: [{ chunk_id, title_path, preview, matches: [{chunk_id, score, cosine, keyword, title_path, preview}, ...] }, ...]
    - unmatched_b: docA側のどの候補にも使われなかった docB の chunk_id リスト

    注意:
    - これは LLM 差分抽出ではありません（Embedding/キーワードの類似度で候補を作るだけ）。
    """
    if not COMPARE_STATE:
        return json.dumps({"ok": False, "error": "先に compare_setup を実行してください。"}, ensure_ascii=False, indent=2)

    res = match_all_chunks(
        chunks_a=COMPARE_STATE["chunks_a"],
        index_b=COMPARE_STATE["index_b"],
        top_k=int(top_k),
        alpha=float(alpha),
        beta=float(beta),
        min_score=float(min_score),
    )

    # 直近のグルーピング結果を保存（エージェントが再利用できるように）
    COMPARE_STATE["last_matching"] = res

    return json.dumps(res, ensure_ascii=False, indent=2)

@tool
def compare_get_grouping(
    which: str,
    mode: str,
    chunk_ids: Optional[List[str]],
    min_similarity: Optional[float],
    max_groups: int,
    max_matches: int,

) -> str:
    """
    「全チャンク類似度マッチング結果（グルーピング）」を取得します。

    重要:
    - 既に作成済みのグルーピング結果を確認するにはこのツールを呼びます。

    取得対象:
    - which="initial": セルのwarm-upで作った `COMPARE_STATE["initial_matching"]`
    - which="last": `compare_all_chunk_similarity_matching` の直近実行結果 `COMPARE_STATE["last_matching"]`

    モード:
    - mode="summary"（デフォルト）: RAG向けの統計情報のみを返却（チャンク詳細なし、コンテキスト節約）
      → 類似度の分布、マッチ/未マッチ数、スコア帯別の統計などを取得
    - mode="detail": 具体的なチャンク情報を返却（従来の動作）
      → chunk_ids または min_similarity でフィルタ可能

    引数:
    - which: str = Field(..., description="取得するグルーピング: 'initial'（warm-upの結果）または 'last'（直近実行の結果）")
    - mode: str = Field("summary", description="取得モード: 'summary'（統計情報のみ）または 'detail'（チャンク詳細）")
    - chunk_ids: Optional[List[str]] = Field(None, description="[mode=detail時] 指定したchunk_id（A側）のみ返却。未指定なら全て対象")
    - min_similarity: Optional[float] = Field(None, description="[mode=detail時] この類似度以上のマッチのみ返却（例: 0.7）")
    - max_groups: int = Field(50, description="[mode=detail時] 返す groups の最大数")
    - max_matches: int = Field(1, description="[mode=detail時] 各 group 内で返す matches の最大数")

    出力(JSON文字列):
    - ok: bool
    - which: "initial_matching" | "last_matching"
    
    [mode=summary時の追加フィールド]
    - statistics: {
        total_chunks_a: docAのチャンク総数,
        total_chunks_b: docBのチャンク総数,
        matched_chunks_a: B側に1つ以上マッチがあるAチャンク数,
        unmatched_chunks_b: A側のどの候補にも使われなかったBチャンク数,
        unmatched_b_ids: 未マッチBチャンクのIDリスト,
        score_distribution: { "0.9-1.0": n, "0.8-0.9": n, ... },
        top_score: 最高スコア,
        bottom_score: 最低スコア,
        avg_score: 平均スコア,
        high_confidence_pairs: スコア0.9以上のペア数,
        medium_confidence_pairs: スコア0.7-0.9のペア数,
        low_confidence_pairs: スコア0.7未満のペア数,
      }
    - hint: 次のアクションのヒント
    
    [mode=detail時の追加フィールド]
    - grouping: フィルタ済みのグループ詳細

    注意:
    - 最初は mode="summary" で統計を把握し、必要に応じて mode="detail" + chunk_ids/min_similarity で詳細を取得してください。
    """
    if not COMPARE_STATE:
        return json.dumps({"ok": False, "error": "先に compare_setup を実行してください。"}, ensure_ascii=False, indent=2)

    w = str(which).lower().strip()
    key = "initial_matching" if w == "initial" else "last_matching"
    grouping = COMPARE_STATE.get(key)
    if grouping is None and key != "initial_matching":
        grouping = COMPARE_STATE.get("initial_matching")
        key = "initial_matching"

    if not isinstance(grouping, dict):
        return json.dumps(
            {
                "ok": False,
                "error": "グルーピング結果が未作成、または形式が不正です。compare_all_chunk_similarity_matching を実行してください。",
                "requested": w,
            },
            ensure_ascii=False,
            indent=2,
        )

    groups = grouping.get("groups", [])
    unmatched_b = grouping.get("unmatched_b", [])
    
    # mode="summary" の場合: 統計情報のみを返却
    if str(mode).lower().strip() == "summary":
        # 統計情報を計算
        all_scores = []
        matched_a_count = 0
        score_bands = {
            "0.9-1.0": 0,
            "0.8-0.9": 0,
            "0.7-0.8": 0,
            "0.6-0.7": 0,
            "0.5-0.6": 0,
            "0.0-0.5": 0,
        }
        
        for g in groups:
            if isinstance(g, dict):
                matches = g.get("matches", [])
                if matches:
                    matched_a_count += 1
                    for m in matches:
                        if isinstance(m, dict):
                            score = m.get("score", 0)
                            all_scores.append(score)
                            # スコア帯に分類
                            if score >= 0.9:
                                score_bands["0.9-1.0"] += 1
                            elif score >= 0.8:
                                score_bands["0.8-0.9"] += 1
                            elif score >= 0.7:
                                score_bands["0.7-0.8"] += 1
                            elif score >= 0.6:
                                score_bands["0.6-0.7"] += 1
                            elif score >= 0.5:
                                score_bands["0.5-0.6"] += 1
                            else:
                                score_bands["0.0-0.5"] += 1
        
        # 統計値を計算
        top_score = max(all_scores) if all_scores else 0.0
        bottom_score = min(all_scores) if all_scores else 0.0
        avg_score = sum(all_scores) / len(all_scores) if all_scores else 0.0
        
        high_conf = sum(1 for s in all_scores if s >= 0.9)
        medium_conf = sum(1 for s in all_scores if 0.7 <= s < 0.9)
        low_conf = sum(1 for s in all_scores if s < 0.7)
        
        statistics = {
            "total_chunks_a": len(groups),
            "total_chunks_b": len(COMPARE_STATE.get("chunks_b", [])),
            "matched_chunks_a": matched_a_count,
            "unmatched_chunks_a": len(groups) - matched_a_count,
            "unmatched_chunks_b": len(unmatched_b),
            "unmatched_b_ids": unmatched_b,
            "score_distribution": score_bands,
            "top_score": round(top_score, 4),
            "bottom_score": round(bottom_score, 4),
            "avg_score": round(avg_score, 4),
            "high_confidence_pairs": high_conf,
            "medium_confidence_pairs": medium_conf,
            "low_confidence_pairs": low_conf,
        }
        
        # ヒントを生成
        hints = []
        if high_conf > 0:
            hints.append(f"高信頼ペア({high_conf}件)は compare_specified_chunks_diff で差分確認推奨")
        if low_conf > 0:
            hints.append(f"低信頼ペア({low_conf}件)は compare_specified_chunks_llm で詳細分析推奨")
        if unmatched_b:
            hints.append(f"未マッチB({len(unmatched_b)}件)は新規追加の可能性。compare_get_chunk で確認推奨")
        if not hints:
            hints.append("全体的に高い類似度。軽微な差異の確認には compare_specified_chunks_diff を使用")
        
        return json.dumps(
            {
                "ok": True,
                "which": key,
                "mode": "summary",
                "statistics": statistics,
                "hint": " / ".join(hints),
            },
            ensure_ascii=False,
            indent=2,
        )
    
    # mode="detail" の場合: 従来の動作（フィルタ機能付き）
    out = dict(grouping)
    filtered_groups = []
    
    # chunk_ids フィルタ
    target_chunk_ids = set(chunk_ids) if chunk_ids else None
    
    mg = max(0, int(max_groups))
    mm = max(0, int(max_matches))
    
    for g in groups:
        if isinstance(g, dict):
            # chunk_ids フィルタ
            if target_chunk_ids and g.get("chunk_id") not in target_chunk_ids:
                continue
            
            g2 = dict(g)
            # preview フィールドを削除（コンテキスト圧迫防止）
            g2.pop("preview", None)
            matches = g2.get("matches", [])
            
            if isinstance(matches, list):
                # min_similarity フィルタ
                if min_similarity is not None:
                    matches = [m for m in matches if isinstance(m, dict) and m.get("score", 0) >= min_similarity]
                
                # matches の各要素からも preview を削除
                matches_trimmed = []
                for m in (matches[:mm] if mm else matches):
                    if isinstance(m, dict):
                        m2 = dict(m)
                        m2.pop("preview", None)
                        matches_trimmed.append(m2)
                    else:
                        matches_trimmed.append(m)
                g2["matches"] = matches_trimmed
            
            # マッチがある場合のみ追加（min_similarityでフィルタ後に空になった場合はスキップ）
            if g2.get("matches") or min_similarity is None:
                filtered_groups.append(g2)
        else:
            filtered_groups.append(g)
        
        # max_groups に達したら終了
        if mg and len(filtered_groups) >= mg:
            break
    
    out["groups"] = filtered_groups
    out["unmatched_b"] = unmatched_b
    
    return json.dumps(
        {
            "ok": True,
            "which": key,
            "mode": "detail",
            "filter_applied": {
                "chunk_ids": chunk_ids,
                "min_similarity": min_similarity,
                "max_groups": mg,
                "max_matches": mm,
            },
            "grouping": out,
        },
        ensure_ascii=False,
        indent=2,
    )


@tool
def compare_search_by_keyphrase(phrase: str, intent: str, in_doc: str = "B", top_k: int = 5, alpha: float = 0.7, min_score: Optional[float] = None) -> str:
    """
    キーフレーズ（複数の単語/文章）でチャンクを検索し、関連度順に返します。

    使いどころ:
    - "勘定科目" など特定トピックがどこに書かれているか当たりを付けたい
    - 全体マッチング結果（compare_all_chunk_similarity_matching）に加えて、ピンポイント探索したい

    検索対象:
    - in_doc="A" なら docA のチャンク集合
    - in_doc="B" なら docB のチャンク集合

    スコア:
    - score = alpha * cosine_similarity(embedding) + (1-alpha) * keyword_match

    引数:
    - phrase: str = Field(..., description="検索クエリ（キーフレーズ）※複数のフレーズを指定する場合はスペース区切りで設定")
    - intent: str = Field(..., description="ツール実行の目的")
    - in_doc: str = Field("B", description="検索対象: 'A' or 'B'")
    - top_k: int = Field(5, description="返す件数")
    - alpha: float = Field(0.7, description="score = alpha*cosine + (1-alpha)*keyword")
    - min_score: Optional[float] = Field(None, description="下限スコア（省略可）")

    出力(JSON文字列):
    - ok: bool
    - results: [{chunk_id, score, cosine, keyword, title_path, preview}, ...]

    """
    if not COMPARE_STATE:
        return json.dumps({"ok": False, "error": "先に compare_setup を実行してください。"}, ensure_ascii=False, indent=2)

    idx = COMPARE_STATE["index_b"] if str(in_doc).upper() == "B" else COMPARE_STATE["index_a"]
    hits = idx.search(query=str(phrase), top_k=int(top_k), alpha=float(alpha), min_score=min_score)
    out = [
        {
            "chunk_id": h.chunk.chunk_id,
            "score": h.score,
            "cosine": h.cosine,
            "keyword": h.keyword,
            "title_path": h.chunk.title_path,
            "preview": h.chunk.preview(),
        }
        for h in hits
    ]
    return json.dumps({"ok": True, "results": out}, ensure_ascii=False, indent=2)

    
@tool
def compare_get_chunk(chunk_ids: List[str], intent: str, in_doc: str = "A") -> str:
    """
    指定した `chunk_ids` のチャンク本文（leafの `content`）とメタ情報を返します。
    最大5チャンクまで一度に指定できます。

    使いどころ:
    - `compare_all_chunk_similarity_matching` / `compare_search_by_keyphrase` で得た chunk_id の中身確認
    - LLM差分抽出（compare_specified_chunks_llm）の前に、どの文章を比較するか人間が確認

    引数:
    - chunk_ids: List[str] = Field(..., description="chunk_idのリスト（最大5件）")
    - intent: str = Field(..., description="ツール実行の目的")
    - in_doc: str = Field("A", description="'A' or 'B'")
    
    出力(JSON文字列):
    - ok: bool
    - chunks: [{ chunk_id, doc_id, node_path, title_path, content }, ...]

    """
    if not COMPARE_STATE:
        return json.dumps({"ok": False, "error": "先に compare_setup を実行してください。"}, ensure_ascii=False, indent=2)

    # chunk_ids をリストとして正規化
    if isinstance(chunk_ids, str):
        chunk_ids = [chunk_ids]
    chunk_ids = [str(cid) for cid in chunk_ids]

    # 最大5チャンクまでのバリデーション
    if len(chunk_ids) > 5:
        return json.dumps({"ok": False, "error": f"chunk_ids は最大5件までです。指定された件数: {len(chunk_ids)}"}, ensure_ascii=False, indent=2)
    
    if len(chunk_ids) == 0:
        return json.dumps({"ok": False, "error": "chunk_ids を1件以上指定してください。"}, ensure_ascii=False, indent=2)

    store = COMPARE_STATE["chunks_a_by_id"] if str(in_doc).upper() == "A" else COMPARE_STATE["chunks_b_by_id"]
    
    results = []
    not_found = []
    for chunk_id in chunk_ids:
        ch = store.get(chunk_id)
        if ch is None:
            not_found.append(chunk_id)
        else:
            results.append({
                "chunk_id": ch.chunk_id,
                "doc_id": ch.doc_id,
                "node_path": ch.node_path,
                "title_path": ch.title_path,
                "content": ch.content,
            })
    
    if not_found:
        return json.dumps({
            "ok": False, 
            "error": f"chunk_id が見つかりません: {', '.join(not_found)}", 
            "in_doc": in_doc,
            "found_chunks": results
        }, ensure_ascii=False, indent=2)

    return json.dumps(
        {
            "ok": True,
            "chunks": results,
        },
        ensure_ascii=False,
        indent=2,
    )


@tool
def compare_specified_chunks_llm(
    chunk_ids_a: List[str],
    chunk_ids_b: List[str],
    intent: str,
    llm_model: Optional[str] = None,
    max_input_chars_per_text: int = 12000,
    max_chars_per_content: int = 10000,
) -> str:
    """
    【チャンク間の類似度が0.7未満の場合推奨】
    指定チャンク同士（A×B）を比較し、差分を構造化JSONとして返します。
    ※比較処理の削減のため、できるだけ関連する複数のチャンクをまとめて1回の呼び出しで設定して実行してください。

    比較対象:
    - 各チャンクの本文（`content`）
    - 上位階層の要約（`content_summary`）
    - 上位チャンク（非leaf）の場合は配下のcontent_summary+contentを全て含める
    
    ※複数チャンク指定時は親子関係を考慮して重複を統合します
   
    入力:
    - chunk_ids_a: docA側の chunk_id リスト
    - chunk_ids_b: docB側の chunk_id リスト
    - intent: ツール実行の目的
    - max_input_chars_per_text: 1テキストあたりの最大投入文字数（長文は末尾を切り詰め）
    - max_chars_per_content: 各contentの最大文字数（超過分は切り詰め、参照メッセージ付与）

    出力(JSON文字列):
    - ok: bool
    - mode: 1:1 | 1:N | N:1 | N:N
    - pairs: [{ chunk_id_a, chunk_id_b, title_path_a, title_path_b, llm: { ok, chat_id, diff } }, ...]
      - diff は { summary, changes:[{type/topic/before/after/evidence_a/evidence_b/impact/confidence}, ...] } 形式
    """
    if not COMPARE_STATE:
        return json.dumps({"ok": False, "error": "先に compare_setup を実行してください。"}, ensure_ascii=False, indent=2)

    res = compare_chunks(
        ast_a=COMPARE_STATE["ast_a"],
        ast_b=COMPARE_STATE["ast_b"],
        chunks_a_by_id=COMPARE_STATE["chunks_a_by_id"],
        chunks_b_by_id=COMPARE_STATE["chunks_b_by_id"],
        chunk_ids_a=chunk_ids_a,
        chunk_ids_b=chunk_ids_b,
        llm_model=llm_model,
        max_input_chars_per_text=int(max_input_chars_per_text),
        max_chars_per_content=int(max_chars_per_content),
    )
    return json.dumps(res, ensure_ascii=False, indent=2)


@tool
def compare_specified_chunks_diff(
    chunk_ids_a: List[str],
    chunk_ids_b: List[str],
    intent: str,
    context_lines: int = 3,
    max_diff_chars: int = 12000,
    max_chars_per_content: int = 10000,
) -> str:
    """
    【チャンク間の類似度が0.7以上の場合推奨】
    指定チャンク同士（A×B）の content を unified diff で比較し、そのまま返します（LLM不要）。
    
    ※content_summaryは使用しません（LLM生成のため必ず差分になるため）
    ※上位チャンク（非leaf）が指定された場合は、配下のcontentを全て結合して比較します
    ※複数チャンクが指定された場合は、親子関係を考慮して重複を統合します

    入力:
    - chunk_ids_a: docA側の chunk_id リスト
    - chunk_ids_b: docB側の chunk_id リスト
    - intent: ツール実行の目的
    - context_lines: unified diff の前後コンテキスト行数
    - max_diff_chars: diff文字列の最大長（超過分は切り詰め）
    - max_chars_per_content: 各contentの最大文字数（超過分は切り詰め、参照メッセージ付与）

    出力(JSON文字列):
    - ok: bool
    - mode: 1:1 | 1:N | N:1 | N:N
    - chunk_ids_a: 比較に使用したAのチャンクIDリスト
    - chunk_ids_b: 比較に使用したBのチャンクIDリスト
    - title_paths_a: Aの各チャンクのtitle_pathリスト
    - title_paths_b: Bの各チャンクのtitle_pathリスト
    - diff: マージされたテキスト間のunified diff
    - truncated: diffが切り詰められたかどうか
    """
    if not COMPARE_STATE:
        return json.dumps({"ok": False, "error": "先に compare_setup を実行してください。"}, ensure_ascii=False, indent=2)

    a_ids = [str(x) for x in (chunk_ids_a or [])]
    b_ids = [str(x) for x in (chunk_ids_b or [])]
    if not a_ids or not b_ids:
        return json.dumps(
            {"ok": False, "error": "chunk_ids_a と chunk_ids_b はそれぞれ1件以上指定してください。"},
            ensure_ascii=False,
            indent=2,
        )

    import difflib
    from .ast_compare import build_merged_compare_text_for_diff

    n = max(0, int(context_lines))
    max_chars = int(max_diff_chars) if (max_diff_chars is not None) else 0
    max_content_chars = int(max_chars_per_content) if (max_chars_per_content is not None) else 0

    # チャンクを収集
    chunks_a = []
    missing_a = []
    for a_id in a_ids:
        ch = COMPARE_STATE["chunks_a_by_id"].get(a_id)
        if ch is None:
            missing_a.append(a_id)
        else:
            chunks_a.append(ch)

    chunks_b = []
    missing_b = []
    for b_id in b_ids:
        ch = COMPARE_STATE["chunks_b_by_id"].get(b_id)
        if ch is None:
            missing_b.append(b_id)
        else:
            chunks_b.append(ch)

    # エラーがあれば報告
    if missing_a or missing_b:
        errors = []
        if missing_a:
            errors.append(f"docA chunk_id が見つかりません: {', '.join(missing_a)}")
        if missing_b:
            errors.append(f"docB chunk_id が見つかりません: {', '.join(missing_b)}")
        return json.dumps({"ok": False, "error": "; ".join(errors)}, ensure_ascii=False, indent=2)

    if not chunks_a or not chunks_b:
        return json.dumps({"ok": False, "error": "有効なチャンクがありません。"}, ensure_ascii=False, indent=2)

    # 複数チャンクをマージ（diff用：contentのみ、content_summary除外、重複統合）
    text_a = build_merged_compare_text_for_diff(
        ast=COMPARE_STATE["ast_a"],
        chunks=chunks_a,
        max_chars_per_content=max_content_chars,
    )
    text_b = build_merged_compare_text_for_diff(
        ast=COMPARE_STATE["ast_b"],
        chunks=chunks_b,
        max_chars_per_content=max_content_chars,
    )

    a_lines = text_a.splitlines(keepends=True)
    b_lines = text_b.splitlines(keepends=True)

    diff_lines = difflib.unified_diff(
        a_lines,
        b_lines,
        fromfile=f"A:[{','.join(a_ids)}]",
        tofile=f"B:[{','.join(b_ids)}]",
        n=n,
    )
    diff_text = "".join(diff_lines)

    truncated = False
    if max_chars > 0 and len(diff_text) > max_chars:
        diff_text = diff_text[:max_chars].rstrip("\n") + "\n... [truncated] ...\n"
        truncated = True

    mode = "N:N"
    if len(a_ids) == 1 and len(b_ids) == 1:
        mode = "1:1"
    elif len(a_ids) == 1 and len(b_ids) > 1:
        mode = "1:N"
    elif len(a_ids) > 1 and len(b_ids) == 1:
        mode = "N:1"

    return json.dumps(
        {
            "ok": True,
            "mode": mode,
            "chunk_ids_a": a_ids,
            "chunk_ids_b": b_ids,
            "title_paths_a": [ch.title_path for ch in chunks_a],
            "title_paths_b": [ch.title_path for ch in chunks_b],
            "diff": diff_text,
            "truncated": truncated,
        },
        ensure_ascii=False,
        indent=2,
    )
