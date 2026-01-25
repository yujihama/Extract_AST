from __future__ import annotations

import pytest

from src.line_segmenter import plan_to_markdown, split_lines, validate_plan
from src.markdown_ast_builder import build_ast_from_markdown
from src.schema import LineSegmentationPlan, LineSegmentationSection


def test_line_segmenter_markdown_inserts_headings() -> None:
    text = "\n".join(["Intro", "Section A", "Body A", "Section B", "Body B"])
    lines = split_lines(text)
    plan = LineSegmentationPlan(
        line_count=len(lines),
        sections=[
            LineSegmentationSection(insert_before_line=2, level=1),
            LineSegmentationSection(insert_before_line=4, level=2),
        ],
    )
    validate_plan(plan, len(lines))
    md = plan_to_markdown(lines, plan)
    md_lines = md.splitlines()

    assert md_lines[1] == "# Section A"
    assert md_lines[4] == "## Section B"

    ast = build_ast_from_markdown(text_content=md)
    root = ast["root"]
    assert len(root["children"]) == 1
    assert root["children"][0]["section_title"] == "Section A"
    assert root["children"][0]["children"][0]["section_title"] == "Section B"


def test_line_segmenter_validate_plan_rejects_invalid() -> None:
    plan_dup = LineSegmentationPlan(
        line_count=2,
        sections=[
            LineSegmentationSection(insert_before_line=1, level=1),
            LineSegmentationSection(insert_before_line=1, level=2),
        ],
    )
    with pytest.raises(ValueError):
        validate_plan(plan_dup, 2)

    plan_unsorted = LineSegmentationPlan(
        line_count=3,
        sections=[
            LineSegmentationSection(insert_before_line=3, level=1),
            LineSegmentationSection(insert_before_line=2, level=1),
        ],
    )
    with pytest.raises(ValueError):
        validate_plan(plan_unsorted, 3)

    plan_bad_level = LineSegmentationPlan(
        line_count=3,
        sections=[LineSegmentationSection(insert_before_line=2, level=7)],
    )
    with pytest.raises(ValueError):
        validate_plan(plan_bad_level, 3)

    plan_out_of_range = LineSegmentationPlan(
        line_count=3,
        sections=[LineSegmentationSection(insert_before_line=4, level=1)],
    )
    with pytest.raises(ValueError):
        validate_plan(plan_out_of_range, 3)
