# Document Compare Agent

文書（PDF/テキスト）を構造化（AST: Abstract Syntax Tree）し、AIエージェントを用いて2文書間の差分を分析するPoCプロジェクトです。

## 概要

このツールは、有価証券報告書や社内規定書などの複雑な構造を持つ文書を：

1. **Blueprint**（階層構造の抽出ルール）に基づいてAST化
2. **Embedding + キーワード検索**で類似チャンクをマッチング
3. **LLM/diff**で差分を抽出・レポート化

することで、人手による文書比較の効率化を目指します。

## 将来的な実装フロー（目標）

このPoCは以下のフローを実現するアプリケーションの前段階検証コードです：

```
1. アップロードドキュメント
2. 既存のBlueprintテンプレートとプレビュー表示
3. 最適なテンプレート選択（今は手動、今後AI）なければ新規生成
4. Blueprintをもとに自動AST化と枝ごとのサマリ生成
5. チャンク戦略の選択（今は手動、今後AI）
6. 比較観点の挿入（人が入力、AIが提案？、プリセット？）
7. 比較観点をもとに手順と結果テンプレート生成
8. 比較実施、テンプレート更新
9. ユーザーに表示
10. ユーザーから追加指示や質問あれば対応
```

> **Note**: 現時点のPoCでは上記フローの一部（特に4〜8）を検証しています。

---

## 環境構築

### 必要なパッケージ

```bash
pip install langchain langchain-core langchain-openai langgraph pydantic python-dotenv pypdf pymupdf deepagents
```

### 環境変数 (`.env`)

```env
# OpenAI
OPENAI_API_KEY=sk-xxx
OPENAI_MODEL=gpt-5.2
OPENAI_EMBEDDING_MODEL=text-embedding-3-large

# Azure OpenAI（Azure使用時）
LLM_PROVIDER=azure
AZURE_OPENAI_ENDPOINT=https://xxx.openai.azure.com/
AZURE_OPENAI_API_KEY=xxx
AZURE_OPENAI_API_VERSION=2024-02-15-preview
AZURE_OPENAI_DEPLOYMENT_NAME=gpt-5.2
AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME=text-embedding-3-large
```

---

## プロジェクト構成

```
compare_agent/
├── test.ipynb                      # メインのPoCノートブック（後述）
├── ast_compare.py                  # チャンク抽出・類似度検索・差分抽出のコア
├── ast_llm_summarizer.py           # ASTノードへのLLMサマリ付与
├── blueprint_ast_builder.py        # Blueprint→ASTへの変換ロジック
├── blueprint_tools.py              # Blueprint検証用LangChainツール
├── agent_log_analyzer.py           # エージェントログの分析・可視化
├── docs/
│   └── ast_store_tool.md           # ast_storeツールの仕様書
├── data/runs/                      # 過去の実行結果アーカイブ
│   └── 【completed】YYYYMMDD_HHMMSS/
│       ├── agent_debug.jsonl       # エージェント実行ログ
│       ├── agent_debug_report.txt  # レポート
│       └── out/                    # 生成ファイル
└── *.txt / *.pdf / *.ast.json      # サンプルドキュメント
```

---

## 主要コンポーネント

### 1. `blueprint_ast_builder.py`

Blueprintファイル（JSON）とテキストファイルから、AST（抽象構文木）を構築します。

- **Blueprint**: 正規表現ベースで見出しパターンを階層ごとに定義
- **AST出力**: セクションごとの `section_title`, `content`, `content_summary`（後からLLMで付与）を格納

```python
from blueprint_ast_builder import build_ast_from_blueprint

ast = build_ast_from_blueprint(
    blueprint_path="xxx_blueprint.json",
    text_path="xxx.txt",
    out_ast_path="xxx.txt.ast.json"
)
```

### 2. `ast_llm_summarizer.py`

ASTの非リーフノードに対して、LLMで `content_summary` を生成・付与します。

```python
from ast_llm_summarizer import summarize_ast_inplace

summarize_ast_inplace(ast_path="xxx.txt.ast.json")
```

### 3. `ast_compare.py`

2つのASTを比較するためのコア機能：

- **チャンク抽出**: `extract_chunks()` でASTをチャンク（比較単位）に分割
  - `strategy="all_leaf"`: 全リーフノードをチャンク化
  - `strategy="level"`: 指定レベルでチャンク化（文字数による分割あり）
- **Hybrid検索**: `HybridChunkIndex` でEmbedding + キーワードの複合スコア
- **差分抽出**: `compare_chunks()` でLLMによる差分JSON生成

### 4. `agent_log_analyzer.py`

エージェント実行ログ（JSONL）を分析し、`data/runs/` にアーカイブ保存します。

```python
from agent_log_analyzer import analyze_agent_log

report = analyze_agent_log(
    log_file="agent_debug.jsonl",
    agent_result=result2  # エージェント実行結果
)
```

---

## `test.ipynb` の処理フロー

メインのPoCノートブックは以下のセクションで構成されています：

### セル構成と処理内容

| セクション | セル | 処理内容 |
|------------|------|----------|
| **準備** | 3-7 | 環境設定、LLM初期化、ミドルウェア定義 |
| **1. カスタムツール** | 9-12 | テキスト読み取り、正規表現抽出、Pydanticスキーマ定義 |
| **2. スキーマ定義** | 11-12 | `DocumentAST`, `HierarchyRule`, `ValidationRules` 等 |
| **3. エージェント設定** | 13-15 | 構造解析エージェントの構築 |
| **4. 実行（Blueprint生成）** | 16-21 | テキストからBlueprint自動生成 |
| **5. AST変換** | 22-25 | Blueprint + テキスト → AST + サマリ付与 |
| **6. 比較** | 26-40 | 2文書間の差分分析 |

### 詳細フロー

#### Phase 1: 準備（セル3-8）

```python
# 環境変数の読み込み
dotenv.load_dotenv()

# LLMクライアント構築（OpenAI / Azure対応）
def build_llm(**kwargs):
    provider = os.getenv("LLM_PROVIDER", "openai").lower()
    ...

# デバッグログ用ミドルウェア（エージェント動作の記録）
class DebugLoggingMiddleware(AgentMiddleware):
    ...
```

#### Phase 2: カスタムツール定義（セル10-12）

LangChainのToolとして以下を定義：

| ツール | 機能 |
|--------|------|
| `read_text_file` | テキストファイルの部分読み込み |
| `read_text_segment` | 指定位置から指定長のテキスト読み込み |
| `extract_regex_matches` | 正規表現でマッチ抽出（行番号付き） |
| `get_file_length` | ファイル総文字数取得 |
| `preview_blueprint_headings` | Blueprint適用プレビュー |
| `validate_blueprint` | Blueprint検証（gaps/titles/irregular） |

#### Phase 3: 構造解析エージェント（セル15）

```python
agent = create_deep_agent(
    model=llm_complex,
    tools=tools,
    system_prompt=system_prompt,  # 構造発見→検証→マッピングの指示
    response_format=DocumentStructureBlueprint,  # 構造化出力
    subagents=[
        {
            "name": "validate_blueprint_agent",
            "description": "blueprintを検証・修正",
            ...
        },
    ],
)
```

**エージェントの処理フロー**:
1. **調査フェーズ**: テキストからパターン発見（記号、インデント、特徴）
2. **検証フェーズ**: 正規表現の誤検知確認、`validation_rules` 定義
3. **構造化フェーズ**: 階層レベル割り当て、親子関係定義
4. **監査フェーズ**: Blueprint検証サブエージェントで最終確認
5. **出力**: `*_blueprint.json` ファイル生成

#### Phase 4: Blueprint実行（セル17-21）

```python
# PDFの場合はテキスト抽出
target_file = "富士フィルム_有価証券報告書.txt"

# エージェント実行
inputs = {
    "messages": [{"role": "user", "content": query}],
    "files": initial_files,  # 仮想ファイルシステム
}
result = agent.invoke(inputs)

# 結果をJSONファイルに保存
with open(output_file, "w", encoding="utf-8") as f:
    json.dump(status_dict, f, ensure_ascii=False, indent=2)
```

#### Phase 5: AST変換（セル23-25）

```python
# Blueprint + テキスト → AST
ast = bab.build_ast_from_blueprint(
    blueprint_path=output_file,
    text_path=target_file,
    out_ast_path=ast_path,
    max_content_chars_per_node=2000,
)

# LLMでサマリ付与（非リーフノードのみ）
als.summarize_ast_inplace(ast_path=ast_path)
```

#### Phase 6: 比較エージェント（セル28-40）

##### 6.1 比較ツール定義（セル28-29）

| ツール | 機能 |
|--------|------|
| `read_ast` | ASTの効率的な読み込み（summary/outline/chunk/search） |
| `compare_setup` | 比較初期化（チャンク化 + Embedding Index作成） |
| `compare_all_chunk_similarity_matching` | 全チャンク類似度マッチング |
| `compare_get_grouping` | マッチング結果の取得（summary/detail） |
| `compare_search_by_keyphrase` | キーフレーズ検索 |
| `compare_get_chunk` | チャンク本文取得 |
| `compare_specified_chunks_diff` | unified diff比較（類似度0.7以上向け） |
| `compare_specified_chunks_llm` | LLM差分抽出（類似度0.7未満向け） |

##### 6.2 比較エージェントの戦略（セル31）

文書関係性に応じた調査戦略：

| 関係性 | 戦略 | 説明 |
|--------|------|------|
| Fix（微修正） | Strict DirectDiff | 類似度上位1位同士を直接比較 |
| Revision（改訂） | Smart Mapping | 変更履歴＋類似度で追跡比較 |
| Derivative（同型） | Loose Mapping | トピック類似度でマッピング |
| Heterogeneous（異種） | Criteria Extraction | 基準リスト生成→検証 |
| Subset（包含） | Scope Filtering | スコープ限定してMapping |

##### 6.3 マルチエージェント構成（セル37）

```python
agent2 = create_deep_agent(
    model=llm_complex,
    system_prompt=system_prompt_deep,  # 監督エージェント
    subagents=[
        {"name": "compare_general_purpose_agent", ...},  # 汎用準備
        {"name": "compare_agent", ...},                  # 差分抽出
        {"name": "deep_research_agent", ...},            # 深掘り分析
        {"name": "validate_agent", ...},                 # 検証
        {"name": "report_agent", ...},                   # レポート生成
    ],
)
```

##### 6.4 実行例（セル38-39）

```python
# 比較実行
result2 = agent2.invoke(inputs)

# 追加指示
new_message = "更新したテンプレートはfilled_template.mdで出力してください"
for chunk in agent2.stream(...):
    # ストリーミング出力
    ...
```

---

## 出力ファイル

### Blueprint JSON

```json
{
  "hierarchy_structure": [
    {
      "level": 1,
      "name": "Major_Section",
      "regex": "^第[一二三四五六七八九十]+.*$",
      "parent_level": null,
      "validation_rules": {
        "requires_prev_empty_line": true,
        "max_length": 50
      }
    },
    ...
  ],
  "global_exclusion_rules": {
    "page_number": {
      "regex": "^\\d+/\\d+$",
      "description": "ページ番号を除外"
    }
  }
}
```

### AST JSON

```json
{
  "file_name": "xxx.txt",
  "__meta__": {"rev": 0, "updated_at": "...", "generated_from": "xxx_blueprint.json"},
  "root": {
    "section_title": "xxx",
    "content": "...",
    "content_summary": "...",
    "children": [
      {
        "section_title": "第一部 ...",
        "content": "...",
        "content_summary": "...",
        "children": [...]
      }
    ]
  }
}
```

---

## 実行ログの確認

```python
from agent_log_analyzer import analyze_agent_log

# ログ分析 + アーカイブ保存
report = analyze_agent_log(
    log_file="agent_debug.jsonl",
    agent_result=result2
)
print(report)
```

出力先: `data/runs/【completed】YYYYMMDD_HHMMSS/`

---

## 注意事項

- **コンテキスト長**: 大きな文書は分割読み込みを推奨（`read_ast(mode="summary")` で事前確認）
- **Embeddingコスト**: `.embedding_cache.json` でキャッシュ（再実行時のコスト削減）
- **LLM呼び出し**: `compare_specified_chunks_llm` は1回あたりコストが高いため、類似度が高いペアは `compare_specified_chunks_diff` を優先

---

## ライセンス

このプロジェクトはPoCであり、商用利用を想定していません。

