from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_text_lines(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().splitlines()


def _load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _dump_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _atomic_write_text(path: str, text: str, encoding: str = "utf-8") -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding=encoding, newline="\n") as f:
        f.write(text)
    os.replace(tmp_path, path)


def _safe_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    return bool(v)


def _normalize_line(s: str) -> str:
    # 全角スペース→半角スペース、連続空白→1つ、strip
    t = (s or "").replace("\u3000", " ")
    t = re.sub(r"\s+", " ", t).strip()
    return t


@dataclass(frozen=True)
class _Rule:
    level: int
    name: str
    regex: str
    parent_level: Optional[int]
    validation_rules: Dict[str, Any]
    pattern: re.Pattern


@dataclass(frozen=True)
class _HeadingEvent:
    line_number: int  # 1-based
    level: int
    name: str
    title: str


def _compile_rules(blueprint: Dict[str, Any]) -> Tuple[List[_Rule], List[_Rule]]:
    rules_in = blueprint.get("hierarchy_structure") or []
    if not isinstance(rules_in, list):
        raise ValueError("blueprint.hierarchy_structure must be a list")

    meta_rules: List[_Rule] = []
    structural_rules: List[_Rule] = []

    for r in rules_in:
        if not isinstance(r, dict):
            continue
        level = int(r.get("level"))
        name = str(r.get("name") or f"level_{level}")
        regex = str(r.get("regex") or "")
        if not regex.strip():
            continue
        parent_level = r.get("parent_level")
        parent_level_i = None if parent_level is None else int(parent_level)
        vr = r.get("validation_rules") or {}
        if not isinstance(vr, dict):
            vr = {}
        try:
            pattern = re.compile(regex)
        except re.error as e:
            raise ValueError(f"Invalid regex in rule '{name}': {regex}. Error: {e}") from e

        rule = _Rule(
            level=level,
            name=name,
            regex=regex,
            parent_level=parent_level_i,
            validation_rules=vr,
            pattern=pattern,
        )
        if level <= 0:
            meta_rules.append(rule)
        else:
            structural_rules.append(rule)

    # 競合時に「より深い階層」を優先できるように level 降順で評価
    structural_rules.sort(key=lambda x: x.level, reverse=True)
    return meta_rules, structural_rules


def _compile_exclusions(blueprint: Dict[str, Any]) -> List[re.Pattern]:
    ex = blueprint.get("global_exclusion_rules") or {}
    if not isinstance(ex, dict):
        return []
    patterns: List[re.Pattern] = []
    for _name, rec in ex.items():
        if not isinstance(rec, dict):
            continue
        rx = rec.get("regex")
        if not isinstance(rx, str) or not rx.strip():
            continue
        try:
            patterns.append(re.compile(rx))
        except re.error:
            # 除外ルールは壊れていても致命にしない
            continue
    return patterns


def _passes_validation(
    *,
    title: str,
    line_number: int,
    prev_line: Optional[str],
    next_line: Optional[str],
    vr: Dict[str, Any],
) -> bool:
    # 位置スコープ
    min_line = vr.get("min_line")
    max_line = vr.get("max_line")
    if min_line is not None and line_number < int(min_line):
        return False
    if max_line is not None and line_number > int(max_line):
        return False

    # 除外行リスト
    exclude_lines = vr.get("exclude_lines") or []
    if isinstance(exclude_lines, list) and line_number in [int(x) for x in exclude_lines if x is not None]:
        return False

    # 文字列条件
    max_len = vr.get("max_length")
    if max_len is not None and len(title) > int(max_len):
        return False

    must_contain = vr.get("must_contain") or []
    if isinstance(must_contain, list):
        for s in must_contain:
            if s is None:
                continue
            if str(s) not in title:
                return False

    must_not_contain = vr.get("must_not_contain") or []
    if isinstance(must_not_contain, list):
        for s in must_not_contain:
            if s is None:
                continue
            if str(s) in title:
                return False

    must_not_end_with = vr.get("must_not_end_with") or []
    if isinstance(must_not_end_with, list):
        for s in must_not_end_with:
            if s is None:
                continue
            if title.endswith(str(s)):
                return False

    # 近傍条件
    requires_prev_empty_line = vr.get("requires_prev_empty_line")
    if requires_prev_empty_line is not None and _safe_bool(requires_prev_empty_line):
        if prev_line is None or _normalize_line(prev_line) != "":
            return False

    prev_rx = vr.get("prev_line_regex")
    if isinstance(prev_rx, str) and prev_rx.strip():
        try:
            if prev_line is None or re.search(prev_rx, prev_line) is None:
                return False
        except re.error:
            # 壊れている場合は無視
            pass

    next_rx = vr.get("next_line_regex")
    if isinstance(next_rx, str) and next_rx.strip():
        try:
            if next_line is None or re.search(next_rx, next_line) is None:
                return False
        except re.error:
            pass

    return True


def extract_headings_from_blueprint(
    *,
    blueprint_path: Optional[str] = None,
    text_path: Optional[str] = None,
    blueprint_content: Optional[str] = None,
    text_content: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """
    blueprint と text を使って見出しイベントのみを抽出する（AST構築前の検証用）。

    引数:
        blueprint_path: blueprint JSON ファイルのパス（blueprint_content が指定されている場合は不要）。
        text_path: テキストファイルのパス（text_content が指定されている場合は不要）。
        blueprint_content: blueprint JSON の内容（文字列）。仮想ファイルシステムからの読み込み用。
        text_content: テキストの内容（文字列）。仮想ファイルシステムからの読み込み用。

    Returns:
        Tuple[List[Dict], int]:
            - headings: 見出しリスト。各要素は {"line_number", "level", "name", "title"} を持つ。
            - total_lines: テキストファイルの総行数。
    """
    # blueprint の読み込み（content が優先）
    if blueprint_content is not None:
        blueprint = json.loads(blueprint_content)
    elif blueprint_path is not None:
        blueprint = _load_json(blueprint_path)
    else:
        raise ValueError("blueprint_path または blueprint_content のどちらかを指定してください")

    # text の読み込み（content が優先）
    if text_content is not None:
        lines = text_content.splitlines()
    elif text_path is not None:
        lines = _read_text_lines(text_path)
    else:
        raise ValueError("text_path または text_content のどちらかを指定してください")
    total_lines = len(lines)

    meta_rules, structural_rules = _compile_rules(blueprint)
    exclusion_patterns = _compile_exclusions(blueprint)

    anchors: Dict[str, bool] = {}

    def _set_anchor(rule_level: int, rule_name: str) -> None:
        anchors[f"level:{int(rule_level)}"] = True
        anchors[f"name:{str(rule_name)}"] = True

    def _anchor_ok(vr: Dict[str, Any]) -> bool:
        key = vr.get("only_after_first_match_of")
        if not isinstance(key, str) or not key.strip():
            return True
        return bool(anchors.get(key))

    headings: List[Dict[str, Any]] = []

    for i, raw in enumerate(lines):
        line_number = i + 1
        line = raw.rstrip("\n")
        prev_line = lines[i - 1] if i - 1 >= 0 else None
        next_line = lines[i + 1] if i + 1 < len(lines) else None

        # 除外行
        excluded_for_heading = any(p.search(line) is not None for p in exclusion_patterns)

        # meta ルール（アンカー用）
        for mr in meta_rules:
            m = mr.pattern.search(line)
            if not m:
                continue
            title = _normalize_line(m.group(0))
            if not title:
                continue
            if not _passes_validation(
                title=title,
                line_number=line_number,
                prev_line=prev_line,
                next_line=next_line,
                vr=mr.validation_rules,
            ):
                continue
            _set_anchor(mr.level, mr.name)

        if excluded_for_heading:
            continue

        # structural ルール
        for sr in structural_rules:
            if not _anchor_ok(sr.validation_rules):
                continue
            m = sr.pattern.search(line)
            if not m:
                continue
            title = _normalize_line(m.group(0))
            if not title:
                continue
            if not _passes_validation(
                title=title,
                line_number=line_number,
                prev_line=prev_line,
                next_line=next_line,
                vr=sr.validation_rules,
            ):
                continue
            headings.append({
                "line_number": line_number,
                "level": sr.level,
                "name": sr.name,
                "title": title,
            })
            _set_anchor(sr.level, sr.name)
            break

    return headings, total_lines


def validate_blueprint_headings(
    headings: List[Dict[str, Any]],
    total_lines: int,
    gap_threshold: int = 100,
    lines: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    見出しリストに対して検証を行い、問題箇所を返す。

    Args:
        headings: extract_headings_from_blueprint の結果。
        total_lines: テキストファイルの総行数。
        gap_threshold: 見出しがない区間をレポートする閾値（行数）。
        lines: テキストファイルの行リスト（未使用、互換性のため残す）。

    Returns:
        Dict with keys:
            - gaps: 見出しがない大きな区間のリスト [{start_line, end_line, gap_size}]
            - titles_by_level: レベルごとのセクションタイトル一覧 {"L1": [...], "L2": [...], ...}
            - level_skips: 見出しレベルの飛び（例: L2→L4）のリスト
            - stats: 統計情報 {total_headings, levels_count, ...}
    """
    result: Dict[str, Any] = {
        "gaps": [],
        "titles_by_level": {},
        "level_skips": [],
        "stats": {},
    }

    if not headings:
        result["stats"] = {"total_headings": 0, "total_lines": total_lines}
        if total_lines > gap_threshold:
            result["gaps"].append({
                "start_line": 1,
                "end_line": total_lines,
                "gap_size": total_lines,
            })
        return result

    # 統計
    levels_count: Dict[int, int] = {}
    for h in headings:
        lvl = h["level"]
        levels_count[lvl] = levels_count.get(lvl, 0) + 1

    result["stats"] = {
        "total_headings": len(headings),
        "total_lines": total_lines,
        "levels_count": levels_count,
    }

    # 空白検証（大きなギャップ）
    prev_line = 0
    for h in headings:
        gap = h["line_number"] - prev_line - 1
        if gap >= gap_threshold:
            result["gaps"].append({
                "start_line": prev_line + 1,
                "end_line": h["line_number"] - 1,
                "gap_size": gap,
            })
        prev_line = h["line_number"]

    # 最後の見出し〜ファイル末尾
    if total_lines - prev_line >= gap_threshold:
        result["gaps"].append({
            "start_line": prev_line + 1,
            "end_line": total_lines,
            "gap_size": total_lines - prev_line,
        })

    # レベルごとのセクションタイトル一覧（タイトルと行番号を含む）
    titles_by_level: Dict[str, List[Dict[str, Any]]] = {}
    for h in headings:
        lvl_key = f"L{h['level']}"
        if lvl_key not in titles_by_level:
            titles_by_level[lvl_key] = []
        titles_by_level[lvl_key].append({
            "title": h["title"],
            "line": h["line_number"],
        })
    
    # レベル順にソート
    result["titles_by_level"] = dict(sorted(titles_by_level.items(), key=lambda x: int(x[0][1:])))

    # 見出しレベルの連続性チェック（飛びの検出）
    # 直前の見出しから2レベル以上深くなる場合は「飛び」として検出
    for i, h in enumerate(headings):
        current_level = h["level"]
        
        if i == 0:
            # 最初の見出し: L1以外から始まる場合も飛びとして検出
            if current_level > 1:
                result["level_skips"].append({
                    "line": h["line_number"],
                    "title": h["title"],
                    "expected_level": 1,
                    "actual_level": current_level,
                    "skipped_levels": list(range(1, current_level)),
                    "message": f"文書がL{current_level}から開始（L1〜L{current_level - 1}がスキップ）",
                })
        else:
            prev_h = headings[i - 1]
            prev_level = prev_h["level"]
            
            # 深くなる場合のみチェック（L2→L1のような浅くなるケースは正常）
            if current_level > prev_level + 1:
                # 飛びがある: 中間のレベルを全てスキップとして報告
                skipped = list(range(prev_level + 1, current_level))
                result["level_skips"].append({
                    "line": h["line_number"],
                    "title": h["title"],
                    "prev_line": prev_h["line_number"],
                    "prev_title": prev_h["title"],
                    "prev_level": prev_level,
                    "actual_level": current_level,
                    "skipped_levels": skipped,
                    "message": f"L{prev_level}→L{current_level}（L{', L'.join(map(str, skipped))}がスキップ）",
                })

    return result


def format_heading_tree(
    headings: List[Dict[str, Any]],
    max_items: Optional[int] = 200,
) -> str:
    """
    見出しリストをインデント付きテキストツリーとして整形する。

    Args:
        headings: extract_headings_from_blueprint の結果。
        max_items: 出力する最大項目数。Noneの場合は全件出力。

    Returns:
        整形されたテキスト。
    """
    if not headings:
        return "(見出しなし)"

    lines_out: List[str] = []
    for i, h in enumerate(headings):
        if max_items is not None and i >= max_items:
            lines_out.append(f"... ({len(headings) - max_items} 件省略)")
            break
        indent = "  " * (h["level"] - 1)
        lines_out.append(f"{indent}[L{h['level']}] {h['title']} (line {h['line_number']})")

    return "\n".join(lines_out)


def build_ast_from_blueprint(
    *,
    blueprint_path: Optional[str] = None,
    text_path: Optional[str] = None,
    blueprint_content: Optional[str] = None,
    text_content: Optional[str] = None,
    out_ast_path: Optional[str] = None,
    root_title: Optional[str] = None,
    max_content_chars_per_node: int = 2000,
    drop_empty_content: bool = True,
) -> Dict[str, Any]:
    """
    blueprint（hierarchy_structure）と txt を使って、見出しを正規表現で抽出し AST を構築する。

    引数:
        blueprint_path: blueprint JSON ファイルのパス（blueprint_content が指定されている場合は不要）。
        text_path: テキストファイルのパス（text_content が指定されている場合は不要）。
        blueprint_content: blueprint JSON の内容（文字列）。仮想ファイルシステムからの読み込み用。
        text_content: テキストの内容（文字列）。仮想ファイルシステムからの読み込み用。
        out_ast_path: 出力 AST ファイルのパス。
        root_title: ルートノードのタイトル。
        max_content_chars_per_node: ノードあたりの最大 content 文字数。
        drop_empty_content: 空の content を削除するかどうか。

    - level>=1 を AST ノードとして採用
    - content_summary には「見出し行の次行〜次の見出し行の直前」までを格納（長すぎる場合は切り詰め）
    - level==0（メタ）は root.content_summary にまとめて格納
    """
    # blueprint の読み込み（content が優先）
    if blueprint_content is not None:
        blueprint = json.loads(blueprint_content)
    elif blueprint_path is not None:
        blueprint = _load_json(blueprint_path)
    else:
        raise ValueError("blueprint_path または blueprint_content のどちらかを指定してください")

    # text の読み込み（content が優先）
    if text_content is not None:
        lines = text_content.splitlines()
    elif text_path is not None:
        lines = _read_text_lines(text_path)
    else:
        raise ValueError("text_path または text_content のどちらかを指定してください")

    meta_rules, structural_rules = _compile_rules(blueprint)
    exclusion_patterns = _compile_exclusions(blueprint)

    # ルートタイトル
    if root_title is None:
        if text_path is not None:
            base = os.path.basename(text_path)
            root_title = base.replace(".txt", "")
        else:
            root_title = "document"

    # アンカー状態: "level:1" / "name:Meta_..." 等
    anchors: Dict[str, bool] = {}

    def _set_anchor(rule_level: int, rule_name: str) -> None:
        anchors[f"level:{int(rule_level)}"] = True
        anchors[f"name:{str(rule_name)}"] = True

    def _anchor_ok(vr: Dict[str, Any]) -> bool:
        key = vr.get("only_after_first_match_of")
        if not isinstance(key, str) or not key.strip():
            return True
        return bool(anchors.get(key))

    # まず行単位で走査して見出しイベントを抽出
    events: List[_HeadingEvent] = []
    meta_hits: List[str] = []

    for i, raw in enumerate(lines):
        line_number = i + 1
        line = raw.rstrip("\n")
        prev_line = lines[i - 1] if i - 1 >= 0 else None
        next_line = lines[i + 1] if i + 1 < len(lines) else None

        # 除外行は「見出し候補」から外す（本文としては残す）
        excluded_for_heading = any(p.search(line) is not None for p in exclusion_patterns)

        # meta ルール（アンカー用 + ルート要約用）
        for mr in meta_rules:
            m = mr.pattern.search(line)
            if not m:
                continue
            title = _normalize_line(m.group(0))
            if not title:
                continue
            if not _passes_validation(
                title=title,
                line_number=line_number,
                prev_line=prev_line,
                next_line=next_line,
                vr=mr.validation_rules,
            ):
                continue
            _set_anchor(mr.level, mr.name)
            meta_hits.append(title)

        if excluded_for_heading:
            continue

        # structural ルール（深い階層優先）
        for sr in structural_rules:
            if not _anchor_ok(sr.validation_rules):
                continue
            m = sr.pattern.search(line)
            if not m:
                continue
            title = _normalize_line(m.group(0))
            if not title:
                continue
            if not _passes_validation(
                title=title,
                line_number=line_number,
                prev_line=prev_line,
                next_line=next_line,
                vr=sr.validation_rules,
            ):
                continue
            events.append(_HeadingEvent(line_number=line_number, level=sr.level, name=sr.name, title=title))
            _set_anchor(sr.level, sr.name)
            break

    # ASTの骨格を構築
    root_node: Dict[str, Any] = {
        "section_title": root_title,
        # ルートは「最初の見出し以前」の本文（メタ行など）を入れる
        "content": "",
        "content_summary": "",
        "children": [],
    }

    # rule lookup for parent_level
    rule_by_level: Dict[int, _Rule] = {r.level: r for r in (meta_rules + structural_rules)}

    # level -> node (直近)
    last_node_by_level: Dict[int, Dict[str, Any]] = {}
    # event index -> node (contentを後で埋める)
    nodes_in_order: List[Dict[str, Any]] = []

    def _find_parent_node(target_level: Optional[int]) -> Dict[str, Any]:
        """親ノードを探す。見つからなければ階層を遡り、最終的にはルートに到達"""
        if target_level is None:
            return root_node
        # target_level から 1 まで遡って探す
        for lvl in range(target_level, 0, -1):
            if lvl in last_node_by_level:
                return last_node_by_level[lvl]
        return root_node

    for ev in events:
        # content_summary は後段でLLMが付与する想定のため、ここでは空
        node: Dict[str, Any] = {"section_title": ev.title, "content": "", "content_summary": "", "children": []}
        nodes_in_order.append(node)

        rule = rule_by_level.get(ev.level)
        parent_level = rule.parent_level if rule else None

        parent = _find_parent_node(parent_level)
        parent.setdefault("children", []).append(node)

        # 同レベル更新＋より深いレベルをリセット
        last_node_by_level[ev.level] = node
        for k in list(last_node_by_level.keys()):
            if k > ev.level:
                last_node_by_level.pop(k, None)

    # 本文(content) を埋める（次の見出し行まで）
    for idx, ev in enumerate(events):
        start = ev.line_number  # 見出し行(1-based)
        next_start = events[idx + 1].line_number if idx + 1 < len(events) else (len(lines) + 1)
        body_lines = lines[start: next_start - 1]  # 見出しの次行〜次見出しの前行
        body = "\n".join([l.rstrip("\n") for l in body_lines]).strip()
        if drop_empty_content and not body:
            body = ""
        if max_content_chars_per_node is not None and max_content_chars_per_node > 0 and len(body) > max_content_chars_per_node:
            body = body[: max_content_chars_per_node].rstrip() + "\n...(truncated)"
        nodes_in_order[idx]["content"] = body

    # root の content に「最初の見出し以前」を格納（メタ情報を含む）
    if events:
        first_heading_line = events[0].line_number  # 1-based
        pre_lines = lines[: max(0, first_heading_line - 1)]
    else:
        pre_lines = lines
    pre_body = "\n".join([l.rstrip("\n") for l in pre_lines]).strip()
    if drop_empty_content and not pre_body:
        pre_body = ""
    if max_content_chars_per_node is not None and max_content_chars_per_node > 0 and len(pre_body) > max_content_chars_per_node:
        pre_body = pre_body[: max_content_chars_per_node].rstrip() + "\n...(truncated)"
    root_node["content"] = pre_body

    # ファイル名の取得（content が指定されている場合はデフォルト値を使用）
    file_name = os.path.basename(text_path) if text_path else "document.txt"
    generated_from = os.path.basename(blueprint_path) if blueprint_path else "blueprint.json"

    ast: Dict[str, Any] = {
        "file_name": file_name,
        "__meta__": {"rev": 0, "updated_at": _utc_now_iso(), "generated_from": generated_from},
        "root": root_node,
    }

    if out_ast_path:
        _atomic_write_text(out_ast_path, _dump_json(ast))

    return ast


def main() -> int:
    p = argparse.ArgumentParser(description="Build AST JSON from blueprint JSON and text file.")
    p.add_argument("--blueprint", required=True, help="Path to *_blueprint.json")
    p.add_argument("--text", required=True, help="Path to *.txt")
    p.add_argument("--out", required=False, help="Output AST path (default: <text>.ast.json)")
    p.add_argument("--root-title", required=False, help="Root section_title (default: derived from text filename)")
    p.add_argument("--max-content-chars", type=int, default=2000, help="Max chars stored per node content_summary")
    args = p.parse_args()

    out = args.out or f"{args.text}.ast.json"
    build_ast_from_blueprint(
        blueprint_path=args.blueprint,
        text_path=args.text,
        out_ast_path=out,
        root_title=args.root_title,
        max_content_chars_per_node=args.max_content_chars,
    )
    print(f"Wrote AST: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


