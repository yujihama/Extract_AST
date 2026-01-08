from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional


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


def _iter_nodes(root: Dict[str, Any], path: Optional[List[int]] = None):
    """Yield (node, path) preorder. path is indices from root.children."""
    if path is None:
        path = []
    yield root, path
    children = root.get("children") or []
    if not isinstance(children, list):
        return
    for i, ch in enumerate(children):
        if not isinstance(ch, dict):
            continue
        yield from _iter_nodes(ch, path + [i])


def _is_leaf(node: Dict[str, Any]) -> bool:
    children = node.get("children") or []
    return not (isinstance(children, list) and any(isinstance(c, dict) for c in children))


def _gather_descendant_content(node: Dict[str, Any], *, max_chars: int) -> str:
    """node.content が空のときに、子孫の content を連結して要約元にする（上限あり）。"""
    parts: List[str] = []
    remaining = max_chars

    def walk(n: Dict[str, Any]) -> None:
        nonlocal remaining
        if remaining <= 0:
            return
        c = n.get("content")
        if isinstance(c, str) and c.strip():
            s = c.strip()
            if len(s) > remaining:
                parts.append(s[:remaining])
                remaining = 0
                return
            parts.append(s)
            remaining -= len(s)
        children = n.get("children") or []
        if isinstance(children, list):
            for ch in children:
                if isinstance(ch, dict):
                    walk(ch)
                    if remaining <= 0:
                        return

    # 自分は除いて子から
    children = node.get("children") or []
    if isinstance(children, list):
        for ch in children:
            if isinstance(ch, dict):
                walk(ch)
                if remaining <= 0:
                    break

    out = "\n\n".join(parts).strip()
    return out


def _gather_children_summaries_or_content(node: Dict[str, Any], *, max_chars: int) -> str:
    """子ノードの content_summary を優先的に集める。空の場合は content を使う（上限あり）。"""
    parts: List[str] = []
    remaining = max_chars

    children = node.get("children") or []
    if not isinstance(children, list):
        return ""

    for ch in children:
        if remaining <= 0:
            break
        if not isinstance(ch, dict):
            continue

        # content_summary を優先、空の場合は content を使う
        summary = ch.get("content_summary")
        content = ch.get("content")
        
        text = ""
        if isinstance(summary, str) and summary.strip():
            text = summary.strip()
        elif isinstance(content, str) and content.strip():
            text = content.strip()
        
        if text:
            if len(text) > remaining:
                parts.append(text[:remaining])
                remaining = 0
                break
            parts.append(text)
            remaining -= len(text)

    out = "\n\n".join(parts).strip()
    return out


@dataclass(frozen=True)
class SummarizeOptions:
    model: str = "gpt-5-mini"
    max_source_chars: int = 6000
    max_descendant_chars: int = 8000
    summary_style: str = "日本語で1〜3文の簡潔な要約。箇条書きは不要。"
    skip_if_summary_exists: bool = True


def _build_llm(model: Optional[str] = None):
    """
    Notebookの build_llm と同等の環境変数で OpenAI / Azure OpenAI を選択。
    依存: langchain-openai
    """
    provider = (os.getenv("LLM_PROVIDER") or "openai").lower()
    temperature = float(os.getenv("TEMPERATURE") or "0")

    from langchain_openai import AzureChatOpenAI, ChatOpenAI

    model_name = model or os.getenv("OPENAI_MODEL") or os.getenv("MODEL") or "gpt-5-mini"


class SummarizationCancelled(RuntimeError):
    """要約処理中の協調的キャンセル用（compare_app側で CancelledError に変換する）。"""

    pass

    if provider in {"azure", "azureopenai", "azure_openai"}:
        return AzureChatOpenAI(
            azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
            azure_deployment=os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME")
            or os.getenv("AZURE_OPENAI_DEPLOYMENT")
            or model_name,
            api_version=os.getenv("AZURE_OPENAI_API_VERSION") or os.getenv("OPENAI_API_VERSION"),
            api_key=os.getenv("AZURE_OPENAI_API_KEY"),
            # temperature=temperature,
        )

    return ChatOpenAI(
        model=model_name,
        api_key=os.getenv("OPENAI_API_KEY"),
        # temperature=temperature,
    )


def summarize_ast_inplace(
    *,
    ast_path: str,
    options: SummarizeOptions = SummarizeOptions(),
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """
    AST JSON を読み込み、子を持つノードのみ content_summary を LLM で生成して書き戻す。
    最下層（leaf）は content_summary を空のままにする。
    """
    ast = _load_json(ast_path)
    root = ast.get("root")
    if not isinstance(root, dict):
        raise ValueError("Invalid AST: root must be an object")

    # 過剰コスト防止のため、下から上へ
    nodes_with_paths = list(_iter_nodes(root))
    nodes_with_paths.reverse()

    # schema guard: content が無いAST（旧形式）では要約元が無く、何も生成できない
    if not any(isinstance(n, dict) and ("content" in n) for n, _p in nodes_with_paths):
        raise ValueError(
            "このASTには 'content' フィールドがありません（旧形式の可能性）。"
            "先に blueprint_ast_builder でASTを再生成してから要約を実行してください。"
        )

    llm = _build_llm(model=options.model)

    from langchain_core.messages import HumanMessage, SystemMessage

    for node, path in nodes_with_paths:
        if is_cancelled is not None and bool(is_cancelled()):
            raise SummarizationCancelled("cancelled")
        if _is_leaf(node):
            node["content_summary"] = ""
            continue

        existing = node.get("content_summary")
        if options.skip_if_summary_exists and isinstance(existing, str) and existing.strip():
            continue

        title = str(node.get("section_title") or "")
        # root は「文書全体要約」なので、原則として子孫本文を要約元にする
        is_root = path == []
        source = ""
        # 子ノードの content_summary を優先的に使う（空の場合は content を使う）
        source = _gather_children_summaries_or_content(node, max_chars=options.max_descendant_chars)
        # 子ノードから取得できない場合は、自分の content または子孫の content を使う
        if not source:
            if not is_root:
                content = node.get("content")
                source = content.strip() if isinstance(content, str) else ""
            if not source:
                source = _gather_descendant_content(node, max_chars=options.max_descendant_chars)

        if not source:
            node["content_summary"] = ""
            continue

        if len(source) > options.max_source_chars:
            source = source[: options.max_source_chars].rstrip() + "\n...(truncated)"

        child_titles: List[str] = []
        children = node.get("children") or []
        if isinstance(children, list):
            for ch in children:
                if isinstance(ch, dict):
                    ct = ch.get("section_title")
                    if isinstance(ct, str) and ct.strip():
                        child_titles.append(ct.strip())

        sys = (
            "あなたは文書のセクション要約者です。"
            "与えられた本文を読み、指定されたスタイルで要約だけを返してください。"
        )
        user = (
            f"セクションタイトル: {title}\n"
            f"子セクション: {', '.join(child_titles) if child_titles else '(なし)'}\n\n"
            f"本文:\n{source}\n\n"
            f"要約スタイル: {options.summary_style}\n"
            "注意: 要約以外は出力しないこと。"
        )

        if is_cancelled is not None and bool(is_cancelled()):
            raise SummarizationCancelled("cancelled")
        resp = llm.invoke([SystemMessage(content=sys), HumanMessage(content=user)])
        summary = getattr(resp, "content", "")
        if not isinstance(summary, str):
            summary = str(summary)
        node["content_summary"] = summary.strip()

    _atomic_write_text(ast_path, _dump_json(ast))
    return ast


def main() -> int:
    p = argparse.ArgumentParser(description="Fill content_summary in AST JSON using LLM (non-leaf nodes only).")
    p.add_argument("--ast", required=True, help="Path to *.ast.json")
    p.add_argument("--max-source-chars", type=int, default=6000)
    p.add_argument("--max-descendant-chars", type=int, default=8000)
    p.add_argument("--overwrite", action="store_true", help="Overwrite existing non-empty summaries")
    args = p.parse_args()

    opts = SummarizeOptions(
        max_source_chars=int(args.max_source_chars),
        max_descendant_chars=int(args.max_descendant_chars),
        skip_if_summary_exists=(not bool(args.overwrite)),
    )
    summarize_ast_inplace(ast_path=args.ast, options=opts)
    print(f"Updated: {args.ast}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


