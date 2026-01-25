from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional


@dataclass(frozen=True)
class Recipe:
    recipe_id: str
    title: str
    description: str
    doc_constraints: Dict[str, Any] = field(default_factory=dict)
    request_text_template: str = ""
    default_params: Dict[str, Any] = field(default_factory=dict)
    allowed_artifact_prefixes: List[str] = field(default_factory=list)
    visible_artifact_kinds: List[str] = field(default_factory=list)

    def normalize_defaults(self) -> Dict[str, Any]:
        params = dict(self.default_params or {})
        params.setdefault("mode", "real")
        params.setdefault("pdf_mode", "fast")
        params.setdefault("summarize_ast", True)
        return params


def _base_recipe(
    recipe_id: str,
    title: str,
    description: str,
    *,
    doc_min: int,
    doc_max: Optional[int],
    request_text_template: str,
) -> Recipe:
    return Recipe(
        recipe_id=recipe_id,
        title=title,
        description=description,
        doc_constraints={
            "min": doc_min,
            "max": doc_max,
            "notes": "用途に応じて基準/対象の役割を依頼文に明記してください。",
        },
        request_text_template=request_text_template.strip(),
        default_params={
            "mode": "real",
            "pdf_mode": "fast",
            "summarize_ast": True,
        },
        allowed_artifact_prefixes=["out/", "work/template_draft.md"],
        visible_artifact_kinds=["template_filled", "template_draft"],
    )


def list_recipes() -> List[Recipe]:
    return [
        _base_recipe(
            "compliance",
            "準拠評価",
            "基準文書と対象文書の整合性を評価し、根拠付きでレポート化します。",
            doc_min=2,
            doc_max=None,
            request_text_template=(
                "基準: <文書A>\n"
                "対象: <文書B/複数>\n"
                "評価観点: <例: 必須事項/例外/証跡>\n"
                "出力: 適合/不適合/要改善の表 + 根拠箇所"
            ),
        ),
        _base_recipe(
            "gap",
            "ギャップ分析",
            "要求事項と対象文書の不足・未対応を抽出し、改善案を示します。",
            doc_min=2,
            doc_max=None,
            request_text_template=(
                "要求文書: <文書A>\n"
                "対象文書: <文書B/複数>\n"
                "確認範囲: <章/要件>\n"
                "出力: 未対応/部分対応/対応済み + 根拠"
            ),
        ),
        _base_recipe(
            "diff",
            "差分・改訂影響",
            "文書の版差分や変更影響を整理します。",
            doc_min=2,
            doc_max=None,
            request_text_template=(
                "旧版: <文書A>\n"
                "新版: <文書B>\n"
                "出力: 変更点の分類 + 影響/対応タスク"
            ),
        ),
        _base_recipe(
            "extract",
            "抽出・構造化",
            "義務/条件/定義などを抽出し、表またはJSONで整理します。",
            doc_min=1,
            doc_max=None,
            request_text_template=(
                "抽出対象: <文書A>\n"
                "抽出項目: <例: 義務/期限/例外>\n"
                "出力形式: <表 or JSON>"
            ),
        ),
        _base_recipe(
            "summary",
            "要約・ブリーフィング",
            "役割別・目的別の要約を作成します。",
            doc_min=1,
            doc_max=None,
            request_text_template=(
                "対象文書: <文書A>\n"
                "読者: <例: 経営/実務/監査>\n"
                "要約粒度: <短め/詳細>"
            ),
        ),
    ]


def get_recipe(recipe_id: str) -> Optional[Recipe]:
    rid = str(recipe_id or "").strip()
    if not rid:
        return None
    for recipe in list_recipes():
        if recipe.recipe_id == rid:
            return recipe
    return None


def all_recipe_ids() -> Iterable[str]:
    return [r.recipe_id for r in list_recipes()]
