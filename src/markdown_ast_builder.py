from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


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


def _strip_heading_trailing_marks(title: str) -> str:
    if not title:
        return ""
    t = title.rstrip()
    # Allow closing hashes like "# Title ####"
    t = re.sub(r"\s#+\s*$", "", t).rstrip()
    return t


@dataclass(frozen=True)
class _HeadingEvent:
    line_number: int  # 1-based
    level: int
    title: str


def _extract_headings(lines: List[str]) -> List[_HeadingEvent]:
    events: List[_HeadingEvent] = []
    in_code_fence = False
    fence_marker = ""

    for idx, raw in enumerate(lines):
        line_number = idx + 1
        line = raw.rstrip("\n")
        stripped = line.lstrip()

        # Toggle fenced code blocks (``` or ~~~), up to 3-space indent supported
        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = stripped[:3]
            if not in_code_fence:
                in_code_fence = True
                fence_marker = marker
            elif marker == fence_marker:
                in_code_fence = False
                fence_marker = ""
            continue

        if in_code_fence:
            continue

        m = re.match(r"^(#{1,6})(?:[ \t]+|$)(.*)$", stripped)
        if not m:
            continue

        level = len(m.group(1))
        title = _strip_heading_trailing_marks(m.group(2).strip())
        if not title:
            continue
        events.append(_HeadingEvent(line_number=line_number, level=level, title=title))

    return events


def _truncate_text(text: str, max_chars: Optional[int]) -> str:
    if not text:
        return ""
    if max_chars is None or max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n...(truncated)"


def build_ast_from_markdown(
    *,
    text_path: Optional[str] = None,
    text_content: Optional[str] = None,
    out_ast_path: Optional[str] = None,
    root_title: Optional[str] = None,
    max_content_chars_per_node: int = 2000,
    drop_empty_content: bool = True,
) -> Dict[str, Any]:
    if text_content is None and text_path is None:
        raise ValueError("text_path または text_content のどちらかを指定してください")

    if text_content is not None:
        content = text_content
    else:
        content = _read_text(str(text_path))

    lines = content.splitlines()
    events = _extract_headings(lines)

    file_name = os.path.basename(text_path) if text_path else "document.md"
    if root_title is None:
        base = file_name
        for ext in (".md", ".markdown", ".txt"):
            if base.lower().endswith(ext):
                base = base[: -len(ext)]
                break
        root_title = base or "document"

    root_node: Dict[str, Any] = {
        "section_title": root_title,
        "content": "",
        "content_summary": "",
        "children": [],
    }

    # Build tree structure with a stack (level 0 = root)
    stack: List[tuple[int, Dict[str, Any]]] = [(0, root_node)]
    nodes_in_order: List[Dict[str, Any]] = []

    for ev in events:
        node: Dict[str, Any] = {"section_title": ev.title, "content": "", "content_summary": "", "children": []}
        nodes_in_order.append(node)

        while stack and stack[-1][0] >= ev.level:
            stack.pop()
        parent = stack[-1][1] if stack else root_node
        parent.setdefault("children", []).append(node)
        stack.append((ev.level, node))

    # Fill contents for each heading node
    for idx, ev in enumerate(events):
        start = ev.line_number  # heading line (1-based)
        next_start = events[idx + 1].line_number if idx + 1 < len(events) else (len(lines) + 1)
        body_lines = lines[start: next_start - 1]
        body = "\n".join(body_lines).strip()
        if drop_empty_content and not body:
            body = ""
        body = _truncate_text(body, max_content_chars_per_node)
        nodes_in_order[idx]["content"] = body

    # Root content is text before the first heading (or whole text if none)
    if events:
        first_heading_line = events[0].line_number
        pre_lines = lines[: max(0, first_heading_line - 1)]
    else:
        pre_lines = lines
    pre_body = "\n".join(pre_lines).strip()
    if drop_empty_content and not pre_body:
        pre_body = ""
    pre_body = _truncate_text(pre_body, max_content_chars_per_node)
    root_node["content"] = pre_body

    ast: Dict[str, Any] = {
        "file_name": file_name,
        "__meta__": {"rev": 0, "updated_at": _utc_now_iso(), "generated_from": "markdown"},
        "root": root_node,
    }

    if out_ast_path:
        _atomic_write_text(out_ast_path, _dump_json(ast))

    return ast
