from __future__ import annotations

from src.markdown_ast_builder import build_ast_from_markdown


def test_markdown_ast_basic_headings() -> None:
    text = "\n".join(
        [
            "# H1",
            "para1",
            "## H2",
            "para2",
            "# H1b",
            "para3",
        ]
    )
    ast = build_ast_from_markdown(text_content=text)
    root = ast["root"]

    assert len(root["children"]) == 2

    h1 = root["children"][0]
    assert h1["section_title"] == "H1"
    assert h1["content"] == "para1"
    assert len(h1["children"]) == 1
    assert h1["children"][0]["section_title"] == "H2"
    assert h1["children"][0]["content"] == "para2"

    h1b = root["children"][1]
    assert h1b["section_title"] == "H1b"
    assert h1b["content"] == "para3"


def test_markdown_ast_ignores_code_fences() -> None:
    text = "\n".join(
        [
            "# H1",
            "```python",
            "# not heading",
            "x = 1",
            "```",
            "body",
        ]
    )
    ast = build_ast_from_markdown(text_content=text)
    root = ast["root"]
    assert len(root["children"]) == 1

    h1 = root["children"][0]
    assert h1["section_title"] == "H1"
    assert "# not heading" in h1["content"]
    assert "body" in h1["content"]


def test_markdown_ast_no_headings() -> None:
    text = "plain text\nmore text"
    ast = build_ast_from_markdown(text_content=text)
    root = ast["root"]
    assert root["children"] == []
    assert "plain text" in root["content"]
