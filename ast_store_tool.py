from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Tuple

from langchain_core.tools import tool
from pydantic import BaseModel, Field


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def _atomic_write_text(path: str, text: str, encoding: str = "utf-8") -> None:
    """
    Write file atomically (best-effort) using os.replace.
    Works on Windows by writing to a temp sibling file then replacing.
    """
    _ensure_parent_dir(path)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding=encoding, newline="\n") as f:
        f.write(text)
    os.replace(tmp_path, path)


def _load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _dump_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _normalize_path_indices(path_indices: Optional[List[int]]) -> List[int]:
    if path_indices is None:
        return []
    return list(path_indices)


@dataclass(frozen=True)
class _NodeRef:
    node: Dict[str, Any]
    parent: Optional[Dict[str, Any]]
    index_in_parent: Optional[int]


def _get_children_list(node: Dict[str, Any]) -> List[Dict[str, Any]]:
    children = node.get("children")
    if children is None:
        children = []
        node["children"] = children
    if not isinstance(children, list):
        raise ValueError("Invalid AST: 'children' must be a list.")
    return children  # type: ignore[return-value]


def _traverse(ast: Dict[str, Any], node_path: List[int]) -> _NodeRef:
    """
    node_path: list of indices into children from root.
    [] refers to root.
    """
    if "root" not in ast or not isinstance(ast["root"], dict):
        raise ValueError("Invalid AST: missing 'root' object.")

    current = ast["root"]
    parent: Optional[Dict[str, Any]] = None
    idx_in_parent: Optional[int] = None

    for idx in node_path:
        children = _get_children_list(current)
        if idx < 0 or idx >= len(children):
            raise IndexError(f"Invalid path index {idx}; children length is {len(children)}.")
        parent = current
        idx_in_parent = idx
        current = children[idx]
        if not isinstance(current, dict):
            raise ValueError("Invalid AST: node must be an object.")

    return _NodeRef(node=current, parent=parent, index_in_parent=idx_in_parent)


def _make_node(section_title: Optional[str], content_summary: str) -> Dict[str, Any]:
    return {
        "section_title": section_title,
        "content_summary": content_summary,
        "children": [],
    }


def _find_nodes_by_title(
    ast: Dict[str, Any],
    title_query: str,
    *,
    max_results: int,
    case_sensitive: bool,
) -> List[Dict[str, Any]]:
    if not title_query:
        return []

    q = title_query if case_sensitive else title_query.lower()
    results: List[Dict[str, Any]] = []

    def walk(node: Dict[str, Any], path: List[int]) -> None:
        if len(results) >= max_results:
            return
        title = node.get("section_title") or ""
        hay = title if case_sensitive else str(title).lower()
        if q in hay:
            results.append({"path": path, "section_title": node.get("section_title")})
            if len(results) >= max_results:
                return

        for i, child in enumerate(_get_children_list(node)):
            if not isinstance(child, dict):
                continue
            walk(child, path + [i])

    root = ast.get("root")
    if isinstance(root, dict):
        walk(root, [])
    return results


class ASTStoreArgs(BaseModel):
    """
    A single tool to persistently manage a document AST on disk.

    Paths:
    - node paths are lists of child indices from root.
      Example: [] = root, [0] = first top-level section, [0, 2] = 3rd child of 1st top-level section.
    """

    action: Literal[
        "init",
        "load",
        "load_subtree",
        "append_child",
        "upsert_child_by_title",
        "update_node",
        "append_to_summary",
        "find_by_title",
    ] = Field(..., description="Operation to perform on the persisted AST.")

    ast_path: str = Field(
        "ast_state.json",
        description="Path to the AST JSON file (will be created/updated).",
    )

    # init
    file_name: Optional[str] = Field(None, description="Document name for the AST (required for init).")
    root_title: Optional[str] = Field(None, description="Optional title for the root node.")
    root_summary: Optional[str] = Field(
        "",
        description="Root node content_summary. Empty string is allowed, but a short description is recommended.",
    )

    # navigation
    node_path: Optional[List[int]] = Field(None, description="Target node path for load_subtree/update/append.")
    parent_path: Optional[List[int]] = Field(None, description="Parent node path for append/upsert.")

    # node data
    section_title: Optional[str] = Field(None, description="Section title for new node or updated node.")
    content_summary: Optional[str] = Field(None, description="Section content summary for new node or updated node.")
    append_text: Optional[str] = Field(None, description="Text to append to content_summary.")

    # append options
    position: Optional[int] = Field(
        None,
        description="Insert position under parent children. Omit to append at the end.",
    )

    # find options
    title_query: Optional[str] = Field(None, description="Substring to find in section_title.")
    max_results: int = Field(20, description="Max number of matches for find operations.")
    case_sensitive: bool = Field(False, description="Whether title matching is case-sensitive.")


@tool(args_schema=ASTStoreArgs)
def ast_store(
    action: str,
    ast_path: str = "ast_state.json",
    file_name: Optional[str] = None,
    root_title: Optional[str] = None,
    root_summary: str = "",
    node_path: Optional[List[int]] = None,
    parent_path: Optional[List[int]] = None,
    section_title: Optional[str] = None,
    content_summary: Optional[str] = None,
    append_text: Optional[str] = None,
    position: Optional[int] = None,
    title_query: Optional[str] = None,
    max_results: int = 20,
    case_sensitive: bool = False,
) -> str:
    """
    Persisted AST editor. Returns a JSON string response with minimal, token-friendly outputs.
    """
    try:
        node_path_n = _normalize_path_indices(node_path)
        parent_path_n = _normalize_path_indices(parent_path)

        # --- init ---
        if action == "init":
            if not file_name:
                return _dump_json({"ok": False, "error": "file_name is required for action=init"})

            ast: Dict[str, Any] = {
                "file_name": file_name,
                "root": _make_node(root_title or file_name, root_summary or ""),
            }
            _atomic_write_text(ast_path, _dump_json(ast))
            return _dump_json(
                {
                    "ok": True,
                    "action": "init",
                    "ast_path": ast_path,
                    "file_name": file_name,
                    "updated_at": _utc_now_iso(),
                }
            )

        # For other actions, we need an existing AST file.
        if not os.path.exists(ast_path):
            return _dump_json(
                {
                    "ok": False,
                    "error": f"AST file not found: {ast_path}. Call action=init first.",
                }
            )

        ast = _load_json(ast_path)

        # --- load ---
        if action == "load":
            # Full load (may be large; caller can choose subtree load for token safety)
            return _dump_json({"ok": True, "action": "load", "ast": ast})

        # --- load_subtree ---
        if action == "load_subtree":
            ref = _traverse(ast, node_path_n)
            return _dump_json(
                {
                    "ok": True,
                    "action": "load_subtree",
                    "node_path": node_path_n,
                    "node": ref.node,
                }
            )

        # --- find_by_title ---
        if action == "find_by_title":
            q = title_query or ""
            matches = _find_nodes_by_title(
                ast,
                q,
                max_results=max(1, int(max_results)),
                case_sensitive=bool(case_sensitive),
            )
            return _dump_json(
                {
                    "ok": True,
                    "action": "find_by_title",
                    "title_query": q,
                    "matches": matches,
                }
            )

        # --- append_child ---
        if action == "append_child":
            if content_summary is None:
                return _dump_json({"ok": False, "error": "content_summary is required for action=append_child"})

            parent_ref = _traverse(ast, parent_path_n)
            children = _get_children_list(parent_ref.node)

            new_node = _make_node(section_title, content_summary)
            if position is None:
                children.append(new_node)
                new_index = len(children) - 1
            else:
                pos = int(position)
                if pos < 0 or pos > len(children):
                    return _dump_json(
                        {"ok": False, "error": f"position out of range: {pos} (0..{len(children)})"}
                    )
                children.insert(pos, new_node)
                new_index = pos

            _atomic_write_text(ast_path, _dump_json(ast))
            return _dump_json(
                {
                    "ok": True,
                    "action": "append_child",
                    "parent_path": parent_path_n,
                    "new_node_path": parent_path_n + [new_index],
                    "updated_at": _utc_now_iso(),
                }
            )

        # --- upsert_child_by_title ---
        if action == "upsert_child_by_title":
            if not section_title:
                return _dump_json({"ok": False, "error": "section_title is required for action=upsert_child_by_title"})
            if content_summary is None:
                return _dump_json(
                    {"ok": False, "error": "content_summary is required for action=upsert_child_by_title"}
                )

            parent_ref = _traverse(ast, parent_path_n)
            children = _get_children_list(parent_ref.node)

            # find exact title match under the parent
            found_index: Optional[int] = None
            for i, child in enumerate(children):
                if not isinstance(child, dict):
                    continue
                if (child.get("section_title") or "") == section_title:
                    found_index = i
                    break

            if found_index is None:
                children.append(_make_node(section_title, content_summary))
                found_index = len(children) - 1
                op = "created"
            else:
                child = children[found_index]
                existing = str(child.get("content_summary") or "")
                if existing:
                    child["content_summary"] = existing.rstrip() + "\n" + str(content_summary).lstrip()
                else:
                    child["content_summary"] = str(content_summary)
                op = "appended"

            _atomic_write_text(ast_path, _dump_json(ast))
            return _dump_json(
                {
                    "ok": True,
                    "action": "upsert_child_by_title",
                    "parent_path": parent_path_n,
                    "node_path": parent_path_n + [found_index],
                    "op": op,
                    "updated_at": _utc_now_iso(),
                }
            )

        # --- update_node ---
        if action == "update_node":
            if section_title is None and content_summary is None:
                return _dump_json(
                    {"ok": False, "error": "section_title and/or content_summary must be provided for action=update_node"}
                )
            ref = _traverse(ast, node_path_n)
            if section_title is not None:
                ref.node["section_title"] = section_title
            if content_summary is not None:
                ref.node["content_summary"] = content_summary

            _atomic_write_text(ast_path, _dump_json(ast))
            return _dump_json(
                {
                    "ok": True,
                    "action": "update_node",
                    "node_path": node_path_n,
                    "updated_at": _utc_now_iso(),
                }
            )

        # --- append_to_summary ---
        if action == "append_to_summary":
            if append_text is None:
                return _dump_json({"ok": False, "error": "append_text is required for action=append_to_summary"})
            ref = _traverse(ast, node_path_n)
            existing = str(ref.node.get("content_summary") or "")
            if existing:
                ref.node["content_summary"] = existing.rstrip() + "\n" + str(append_text).lstrip()
            else:
                ref.node["content_summary"] = str(append_text)

            _atomic_write_text(ast_path, _dump_json(ast))
            return _dump_json(
                {
                    "ok": True,
                    "action": "append_to_summary",
                    "node_path": node_path_n,
                    "updated_at": _utc_now_iso(),
                }
            )

        return _dump_json({"ok": False, "error": f"Unknown action: {action}"})

    except Exception as e:
        return _dump_json({"ok": False, "error": str(e)})



