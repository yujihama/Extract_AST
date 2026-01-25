from __future__ import annotations

import re
from typing import Iterable, List

from src.schema import LineSegmentationPlan

_LINE_PREFIX_RE = re.compile(r"^L\d{4,}\|")


def split_lines(text: str) -> List[str]:
    return (text or "").splitlines()


def add_line_numbers(lines: Iterable[str], *, width: int | None = None) -> str:
    lines_list = list(lines)
    count = len(lines_list)
    if count <= 0:
        return ""
    if width is None:
        width = max(4, len(str(count)))
    numbered = [f"L{idx:0{width}d}|{line}" for idx, line in enumerate(lines_list, start=1)]
    return "\n".join(numbered)


def validate_plan(plan: LineSegmentationPlan, line_count: int) -> None:
    if line_count < 0:
        raise ValueError("line_count must be >= 0")
    if int(plan.line_count) != int(line_count):
        raise ValueError(f"plan.line_count mismatch: plan={plan.line_count}, actual={line_count}")

    if line_count == 0:
        if plan.sections:
            raise ValueError("sections must be empty when line_count is 0")
        return

    seen: set[int] = set()
    prev = 0
    for sec in plan.sections:
        line_no = int(sec.insert_before_line)
        level = int(sec.level)
        if line_no < 1 or line_no > line_count:
            raise ValueError(f"insert_before_line out of range: {line_no} (1..{line_count})")
        if level < 1 or level > 6:
            raise ValueError(f"level out of range: {level} (1..6)")
        if line_no in seen:
            raise ValueError(f"duplicate insert_before_line: {line_no}")
        if line_no < prev:
            raise ValueError("sections must be sorted by insert_before_line")
        seen.add(line_no)
        prev = line_no


def plan_to_markdown(
    lines: List[str],
    plan: LineSegmentationPlan,
    *,
    title_max_chars: int = 120,
) -> str:
    line_count = len(lines)
    if line_count == 0:
        return ""

    sections_by_line = {int(sec.insert_before_line): int(sec.level) for sec in plan.sections}
    out: List[str] = []

    for idx, line in enumerate(lines, start=1):
        if idx in sections_by_line:
            level = sections_by_line[idx]
            title = _resolve_title(lines, idx - 1, title_max_chars=title_max_chars)
            prefix = "#" * max(1, min(6, level))
            heading = f"{prefix} {title}".rstrip()
            out.append(heading)
        out.append(line)

    return "\n".join(out)


def _resolve_title(lines: List[str], start_index: int, *, title_max_chars: int) -> str:
    if start_index < 0:
        start_index = 0
    for i in range(start_index, len(lines)):
        raw = lines[i]
        candidate = _strip_line_prefix(raw).strip()
        if candidate:
            if title_max_chars > 0 and len(candidate) > title_max_chars:
                return candidate[:title_max_chars].rstrip()
            return candidate
    return ""


def _strip_line_prefix(text: str) -> str:
    return _LINE_PREFIX_RE.sub("", text or "", count=1)
