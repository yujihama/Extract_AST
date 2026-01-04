# -*- coding: utf-8 -*-
"""
Blueprint検証ツール（仮想ファイルシステム対応版）

このモジュールは、エージェントの仮想ファイルシステムを自動的に参照する
preview_blueprint_headings と validate_blueprint ツールを提供します。

使用方法:
    from blueprint_tools import preview_blueprint_headings, validate_blueprint
    
    tools = [..., preview_blueprint_headings, validate_blueprint, ...]
"""

import json
import os
from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import InjectedState

from .blueprint_ast_builder import (
    extract_headings_from_blueprint,
    format_heading_tree,
    validate_blueprint_headings,
)


def _get_file_content_from_state(state: dict | None, file_path: str) -> str | None:
    """
    仮想ファイルシステムからファイル内容を取得する。
    
    Args:
        state: エージェントの状態（filesキーを含む場合がある）
        file_path: ファイルパス（先頭の/はあってもなくても可）
    
    Returns:
        ファイル内容（文字列）、見つからない場合はNone
    """
    if not state or "files" not in state:
        return None
    
    files = state["files"]
    path_clean = file_path.lstrip("/")
    
    # 両方のキー形式を試す（/付きと/なし）
    for key in [f"/{path_clean}", path_clean]:
        if key in files:
            file_data = files[key]
            if isinstance(file_data, dict) and "content" in file_data:
                content = file_data["content"]
                if isinstance(content, list):
                    return "\n".join(content)
                return str(content)
    
    return None


@tool
def preview_blueprint_headings(
    blueprint_path: str,
    text_path: str,
    intent: str = "",
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """
    blueprint と text ファイルから見出しを抽出し、階層構造をツリー形式でプレビューする。
    AST 構築前に「どの見出しがどのレベルで抽出されるか」を確認できる。
    全ての見出しを抽出して返却する。
    
    仮想ファイルシステムを優先して参照します。仮想FSにファイルがない場合は
    実際のファイルシステムから読み込みます。

    引数:
        blueprint_path: blueprint JSON ファイルのパス（例: "文書_blueprint.json"）。
        text_path: テキストファイルのパス（例: "文書.txt"）。
        intent: ツール呼び出しの意図。

    戻り値:
        JSON 形式の結果:
        - ok: 成功/失敗
        - tree: 階層構造を示すインデント付きテキスト（全件）
    """
    try:
        bp_clean = blueprint_path.lstrip("/")
        txt_clean = text_path.lstrip("/")
        
        # 仮想FSから読み込みを試みる
        bp_content = _get_file_content_from_state(state, bp_clean)
        txt_content = _get_file_content_from_state(state, txt_clean)
        
        # content がある場合は優先、なければ実ファイルから
        if bp_content is not None and txt_content is not None:
            headings, total_lines = extract_headings_from_blueprint(
                blueprint_content=bp_content,
                text_content=txt_content,
            )
            bp_display = f"{bp_clean} (virtual)"
            txt_display = f"{txt_clean} (virtual)"
        else:
            bp_normalized = os.path.normpath(bp_clean)
            txt_normalized = os.path.normpath(txt_clean)
            headings, total_lines = extract_headings_from_blueprint(
                blueprint_path=bp_normalized,
                text_path=txt_normalized,
            )
            bp_display = bp_normalized
            txt_display = txt_normalized
        
        # 全件を表示（max_items=Noneで制限なし）
        tree = format_heading_tree(headings, max_items=None)
        
        return json.dumps({
            "ok": True,
            "tree": tree,
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


@tool
def validate_blueprint(
    blueprint_path: str,
    text_path: str,
    mode: str = "gaps",
    gap_threshold: int = 100,
    max_level: int = 3,
    intent: str = "",
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """
    blueprint と text ファイルから見出しを抽出し、指定モードに応じた検証結果を返す。

    モード:
        - "gaps": 見出しがない大きな区間（gap_threshold 行以上）を検出
        - "titles": L1/L2/L3...ごとのセクションタイトル一覧を返却（max_levelで制限可能）
        - "irregular": 見出しレベルの連続性チェック（例: L2→L4のような飛びを検出）
    
    仮想ファイルシステムを優先して参照します。仮想FSにファイルがない場合は
    実際のファイルシステムから読み込みます。

    引数:
        blueprint_path: blueprint JSON ファイルのパス。
        text_path: テキストファイルのパス。
        mode: 検証モード（"gaps", "titles", "irregular"）。デフォルトは "gaps"。
        gap_threshold: 見出しがない区間を報告する閾値（行数、デフォルト: 100）。modeが"gaps"の時のみ使用。
        max_level: (mode="titles"時) 取得する最大レベル。デフォルトは 3（L1〜L3を取得）。0で全レベル取得。
        intent: ツール呼び出しの意図。

    戻り値:
        JSON 形式の検証結果（モードにより内容が異なる）:
        - ok: 成功/失敗
        - mode: 実行したモード
        - stats: 統計情報（total_headings, total_lines, levels_count）
        - gaps: (mode="gaps"時) 見出しがない大きな区間のリスト
        - titles_by_level: (mode="titles"時) レベルごとのセクションタイトル一覧
        - level_skips: (mode="irregular"時) 見出しレベルの飛び（例: L2→L4）のリスト
    """
    valid_modes = {"gaps", "titles", "irregular"}
    if mode not in valid_modes:
        return json.dumps({
            "ok": False,
            "error": f"無効なモードです: '{mode}'。有効なモード: {', '.join(sorted(valid_modes))}"
        }, ensure_ascii=False)
    
    try:
        bp_clean = blueprint_path.lstrip("/")
        txt_clean = text_path.lstrip("/")
        
        # 仮想FSから読み込みを試みる
        bp_content = _get_file_content_from_state(state, bp_clean)
        txt_content = _get_file_content_from_state(state, txt_clean)
        
        # content がある場合は優先、なければ実ファイルから
        if bp_content is not None and txt_content is not None:
            headings, total_lines = extract_headings_from_blueprint(
                blueprint_content=bp_content,
                text_content=txt_content,
            )
            lines = txt_content.splitlines()
        else:
            bp_normalized = os.path.normpath(bp_clean)
            txt_normalized = os.path.normpath(txt_clean)
            headings, total_lines = extract_headings_from_blueprint(
                blueprint_path=bp_normalized,
                text_path=txt_normalized,
            )
            # 実ファイルからテキストを読み込み
            with open(txt_normalized, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        
        # 検証を実行
        validation = validate_blueprint_headings(
            headings=headings,
            total_lines=total_lines,
            gap_threshold=gap_threshold,
            lines=lines,
        )
        
        # レベル別カウント
        levels_count = {}
        for h in headings:
            lvl = h["level"]
            levels_count[lvl] = levels_count.get(lvl, 0) + 1
        
        # 基本結果を構築
        result = {
            "ok": True,
            "mode": mode,
            "stats": {
                "total_headings": len(headings),
                "total_lines": total_lines,
                "levels_count": levels_count,
            },
        }
        
        # モードに応じた結果を追加
        if mode == "gaps":
            result["gaps"] = validation.get("gaps", [])
        elif mode == "titles":
            titles_by_level = validation.get("titles_by_level", {})
            # max_level でフィルタリング（0の場合は全レベル取得）
            if max_level > 0:
                titles_by_level = {
                    k: v for k, v in titles_by_level.items()
                    if int(k[1:]) <= max_level
                }
            result["titles_by_level"] = titles_by_level
            result["max_level"] = max_level if max_level > 0 else "all"
        elif mode == "irregular":
            result["level_skips"] = validation.get("level_skips", [])
        
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)

