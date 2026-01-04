from typing import Optional, Dict, List
from pydantic import BaseModel, Field, field_validator
import re

class DocumentNode(BaseModel):
    section_title: Optional[str] = Field(None, description="セクションのタイトル")
    content_summary: str = Field(..., description="このセクションのコンテンツの簡潔な要約")
    children: List['DocumentNode'] = Field(default_factory=list, description="サブセクションまたはネストされたコンテンツ")


class DocumentAST(BaseModel):
    file_name: str = Field(..., description="解析されたファイル名")
    root: DocumentNode = Field(..., description="文書構造のルートノード")


class AgentResult(BaseModel):
    status: str = Field(..., description="実行ステータス（例: 'ok'）")
    ast_path: str = Field(..., description="永続化された AST JSON のパス")
    note: Optional[str] = Field(None, description="オプションのメッセージ")


# pydantic v2: resolve forward references
DocumentNode.model_rebuild()
DocumentAST.model_rebuild()
AgentResult.model_rebuild()

class ValidationRules(BaseModel):
    """見出し抽出時の誤検知を防ぐための検証ルールセット。

    正規表現マッチ後の「追加チェック」として使用します。

    使い方の目安（後続Workerが実装するときの優先度イメージ）:
    - まず regex にマッチした行を候補にする
    - 次に validation_rules で候補を落とす（= WHERE 句相当）

    例（位置スコープ）:
        {"min_line": 200}  # 200行目以降のみ見出し候補

    例（近傍行条件）:
        {"prev_line_regex": "^$"}  # 直前行が空行のときだけ

    例（アンカー条件）:
        {"only_after_first_match_of": "level:1"}  # Level1が初めて出現した後だけ
    """

    # --- 文字列自体への検証 ---
    max_length: Optional[int] = Field(
        None,
        description="見出しとして許容される最大文字数。これを超えると本文とみなす。",
    )
    must_contain: Optional[List[str]] = Field(
        None,
        description="見出し文字列に含まれていなければならない特定の文字リスト（例: ['【', '】']）。",
    )
    must_not_contain: Optional[List[str]] = Field(
        None,
        description="見出し文字列に含まれていてはいけない文字リスト。",
    )
    must_not_end_with: Optional[List[str]] = Field(
        None,
        description="行末に来てはいけない文字リスト（例: ['。', '.']）。",
    )

    # --- 行の文脈/位置への検証（WHERE句的） ---
    requires_line_start: Optional[bool] = Field(
        True,
        description="正規表現が行頭(^)からマッチする必要があるか。",
    )
    requires_prev_empty_line: Optional[bool] = Field(
        None,
        description="直前の行が空行であることを必須とするか（互換用）。prev_line_regex='^$' で代替可能。",
    )
    min_line: Optional[int] = Field(
        None,
        description="このルールを適用する最小行番号（1始まり、含む）。例: 200 なら 200行目以降のみ候補。",
    )
    max_line: Optional[int] = Field(
        None,
        description="このルールを適用する最大行番号（1始まり、含む）。min_line と併用可能。",
    )
    exclude_lines: Optional[List[int]] = Field(
        None,
        description="除外する行番号のリスト（1始まり）。例: [4506, 4510] なら 4506行目と4510行目は見出し候補から除外。",
    )
    prev_line_regex: Optional[str] = Field(
        None,
        description=(
            "直前行テキストに対する正規表現。マッチしない場合は候補から除外。"
            "例: '^$'（空行）や '^(?!\\s*Page\\b)' など。"
        ),
    )
    next_line_regex: Optional[str] = Field(
        None,
        description=(
            "直後行テキストに対する正規表現。マッチしない場合は候補から除外。"
            "例: '^\\s{2,}\\S'（次行がインデント）など。"
        ),
    )
    only_after_first_match_of: Optional[str] = Field(
        None,
        description=(
            "アンカー条件: 指定したトリガが『最初に出現した後』のみ、このルールを有効にする。"
            "推奨形式: 'level:<int>' または 'name:<str>'。例: 'level:1', 'name:Major_Section'。"
        ),
    )

    # --- 自然言語の追加条件（最後の手段） ---
    context_filters: Optional[List[str]] = Field(
        None,
        description="自然言語で記述された文脈的な判定ロジック。Workerロジックで実装が必要な複雑な条件（最小限に）。",
    )

    @field_validator("min_line", "max_line")
    @classmethod
    def validate_line_number(cls, v: Optional[int]) -> Optional[int]:
        if v is None:
            return None
        if int(v) < 1:
            raise ValueError("line number must be >= 1")
        return int(v)

    @field_validator("exclude_lines")
    @classmethod
    def validate_exclude_lines(cls, v: Optional[List[int]]) -> Optional[List[int]]:
        if v is None:
            return None
        for line_num in v:
            if int(line_num) < 1:
                raise ValueError(f"exclude_lines: line number must be >= 1, got {line_num}")
        return [int(x) for x in v]

    @field_validator("prev_line_regex", "next_line_regex")
    @classmethod
    def validate_neighbor_regex(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        if not isinstance(v, str) or not v.strip():
            return None
        try:
            re.compile(v)
        except re.error as e:
            raise ValueError(f"Invalid neighbor regex: {v}. Error: {e}")
        return v


class HierarchyRule(BaseModel):
    """
    文書の階層構造（Level）ごとの定義ルール。
    """
    level: int = Field(..., description="階層レベル（0: メタ情報, 1: 最上位, ...）")
    name: str = Field(..., description="階層の名称（識別子）")
    regex: str = Field(..., description="この階層を抽出するためのPython正規表現文字列")
    parent_level: Optional[int] = Field(
        None, 
        description="親となる階層レベル。Noneの場合はルート要素（またはそれに準ずる）。"
    )
    description: Optional[str] = Field(None, description="ルールの説明・メモ")
    validation_rules: Optional[ValidationRules] = Field(
        default_factory=ValidationRules,
        description="正規表現マッチ後に適用するバリデーションルール"
    )

    @field_validator('regex')
    @classmethod
    def validate_regex(cls, v: str) -> str:
        """提供された正規表現文字列がPythonでコンパイル可能かチェックする"""
        try:
            re.compile(v)
        except re.error as e:
            raise ValueError(f"Invalid regex pattern: {v}. Error: {e}")
        return v

class ExclusionRule(BaseModel):
    """
    最初から除外すべき行（ページ番号や特定の箇条書きなど）のルール。
    """
    regex: str = Field(..., description="除外対象とする行の正規表現")
    description: Optional[str] = Field(None, description="除外理由の説明")

    @field_validator('regex')
    @classmethod
    def validate_regex(cls, v: str) -> str:
        try:
            re.compile(v)
        except re.error as e:
            raise ValueError(f"Invalid regex pattern: {v}. Error: {e}")
        return v

class DocumentStructureBlueprint(BaseModel):
    """
    エージェントが生成するドキュメント構造定義書のルートモデル。
    """
    hierarchy_structure: List[HierarchyRule] = Field(
        ..., 
        description="階層定義のリスト。レベル順にソートされていることが望ましい。"
    )
    global_exclusion_rules: Optional[Dict[str, ExclusionRule]] = Field(
        default=None,
        description="文書全体で適用される除外ルール（キーはルール名）"
    )

    def get_rule_by_level(self, level: int) -> Optional[HierarchyRule]:
        """指定されたレベルのルールを取得するヘルパーメソッド"""
        for rule in self.hierarchy_structure:
            if rule.level == level:
                return rule
        return None

class PreAnalysisResult(BaseModel):
    """
    - relation: Fix | Revision | Derivative | Heterogeneous | Subset
    - reason: 上記判断に至った理由（内容や構成の分析結果）
    - plan: [手順1, 手順2, ...] ※doc比較の具体的な分析手順
    - template: <Markdown> ※doc比較結果記載用テンプレート（マークダウン形式）
    """
    relation: str = Field(description="本doc間の関係タイプ（Fix/Revision/Derivative/Heterogeneous/Subset）")
    reason: str = Field(description="上記関係タイプを推定した理由。doc内容・章立て・構成・記載範囲の違いなど。")
    plan: list[str] = Field(description="分析実施のための具体手順リスト")
    template: str = Field(description="分析結果記載用テンプレートのファイル名")
