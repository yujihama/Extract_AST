# compare_agent（PoC）

このリポジトリは、**ドキュメント比較アプリ**を将来的に実装するための **PoC（検証用コード）**です。  
現時点ではアプリ全体のフローをすべて実装しているわけではなく、**blueprint→AST→比較**のコア部分を中心に試作しています。

---

## 将来の想定フロー（補足）

将来的に目指すアプリフロー（※PoCでは未実装/一部のみ含みます）:

1. アップロードドキュメント
2. 既存のBlueprintテンプレートとプレビュー表示
3. 最適なテンプレート選択（今は手動、今後AI）／なければ新規生成
4. Blueprintをもとに自動AST化と枝ごとのサマリ生成
5. チャンク戦略の選択（今は手動、今後AI）
6. 比較観点の挿入（人が入力、AIが提案？、プリセット？）
7. 比較観点をもとに手順と結果テンプレート生成
8. 比較実施、テンプレート更新
9. ユーザーに表示
10. ユーザーから追加指示や質問あれば対応

---

## このPoCで主にできること

- **Blueprint生成（LLM）**: テキストから見出しパターンを検出し、`*_blueprint.json` を生成
- **Blueprint検証/プレビュー**: 見出し階層のツリー表示、ギャップ/レベル飛び等の検証
- **Blueprint → AST化（非LLM）**: 正規表現で見出しを抽出し、`*.ast.json`（階層+content）を構築
- **ASTの枝ごとの要約（LLM）**: 非leafノードに `content_summary` を付与
- **チャンク化＆マッチング**: Embedding + キーワードのハイブリッドで対応候補を作る
- **差分抽出**:
  - 高類似度: unified diff（LLM不要）
  - 低類似度: LLMで差分を構造化JSONとして抽出
- **テンプレートに沿った比較レポート生成**: 指定テンプレートを“段階的に編集”して埋める
- **デバッグログ（JSONL）とレポート化**: 実行ログ解析→`data/runs/`に成果物を保存

---

## 主要ファイル/ディレクトリ

- `main.py`: PoCの一連フロー（セル形式 `# %%`）の実行スクリプト
- `src/`
  - `prompt.py`: blueprint生成/比較用のプロンプト
  - `schema.py`: blueprint/比較結果のスキーマ（Pydantic）
  - `blueprint_ast_builder.py`: blueprint + txt から AST JSON を構築
  - `ast_llm_summarizer.py`: ASTの `content_summary` をLLMで埋める
  - `ast_compare.py`: チャンク化、Embedding、類似度マッチング、差分抽出（diff/LLM）
  - `tools.py`: LangChainツール群（`compare_setup`, `analyze_visual_contents` など）
  - `pdf_to_text_llm.py`: PDFをテキストに変換（LLMによる画像解析付きの高度な変換）
  - `blueprint_tools.py`: blueprintのプレビュー/検証ツール（仮想FS対応）
  - `utils.py`: LLM初期化、PDF→txt変換、デバッグログ用ミドルウェア等
  - `agent_log_analyzer.py`: JSONLログ解析→`data/runs/`へ保存
- `templates/`: 比較結果テンプレート（例: `diff_analysis_template_fujifilm_yuho.md`）
- `data/`: 実行成果物（例: `data/runs/`） ※入力/中間生成物用フォルダも想定
- `log/`: JSONLログ（`agent_debug_*.jsonl`）

> 注: `.gitignore` により `data/`, `log/`, `backup/` などは基本的にコミット対象外です。

---

## セットアップ（PowerShell想定）

### 1) 仮想環境

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
```

### 2) 依存関係（例）

`test.ipynb` 内の想定に合わせた例です（環境により追加で必要になる場合があります）。

```powershell
pip install "langchain>=1.0,<2" "langchain-core>=1.0,<2" "langchain-openai" "langgraph" "pydantic>=2,<3" "python-dotenv" "openai" "pymupdf>=1.23.0"
pip install deepagents
```

### 3) 環境変数（`.env`）

OpenAI利用例:

```dotenv
LLM_PROVIDER=openai
OPENAI_API_KEY=your_key
OPENAI_MODEL=gpt-5-mini
OPENAI_EMBEDDING_MODEL=text-embedding-3-large
TEMPERATURE=0
```

Azure OpenAI利用例:

```dotenv
LLM_PROVIDER=azure
AZURE_OPENAI_ENDPOINT=https://xxxxx.openai.azure.com/
AZURE_OPENAI_API_KEY=your_key
AZURE_OPENAI_API_VERSION=2024-xx-xx
AZURE_OPENAI_DEPLOYMENT_NAME=gpt-5-mini
AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME=text-embedding-3-large
TEMPERATURE=0
```

---

## Web UI / CLI（MVP: ダミーPipeline）

`compare_app/` は **FastAPI+HTMX UI** と **CLI** の土台です。まずは「Run作成→実行→イベント追跡→テンプレ生成（ダミー）」まで動作します。

依存関係:

```powershell
pip install -r requirements.txt
```

### CLI（同期実行・テスト向け）

```powershell
# Run作成（入力は任意のPDF/TXTでOK）
python -m compare_app.cli create --doc-a .\data\input\仕訳定義書.txt --doc-b .\data\input\仕訳定義書_文体変更版.txt

# Run実行（run_idを指定）
python -m compare_app.cli execute <run_id>

# イベント追跡
python -m compare_app.cli tail <run_id>
```

### Web UI（FastAPI）

```powershell
uvicorn compare_app.web.app:app --reload
```

ブラウザで `http://127.0.0.1:8000` を開いてください。

## 実行方法（概要）

### `main.py`（推奨: セル順に実行）

`main.py` は `# %%` で区切ったセルを上から順に実行する想定です（Cursor/VS Codeのセル実行）。

- **主な入力**:
  - `data/input/` に比較対象ドキュメント（`.txt` または `.pdf`）を配置
  - `main.py` 内の `target_file`, `docA`, `docB`, `template_compare_analysis` を適宜変更
- **PDFからテキストへの変換**（PDFを入力する場合）:
  - **テキスト中心のPDF**: `convert_pdf_to_txt()` を使用（高速、シンプル）
  - **視覚的な要素が多いPDF**: `convert_pdf_with_llm()` を使用（LLMによる画像解析付き）
  - `main.py` 内でどちらを使用するかコメントで切り替え可能
- **主な出力**:
  - blueprint: `data/blueprint/*_blueprint.json`
  - AST: `data/ast/*.ast.json`
  - embedding cache: `data/embedding/embedding_cache.json`
  - logs: `log/*.jsonl`
  - 実行成果物: `data/runs/【completed】YYYYMMDD_HHMMSS/`

---

## `test.ipynb` の処理順（セルを上から実行したときに何をしているか）

`test.ipynb` は PoC の実験ノートで、**「blueprint生成→AST化→比較→テンプレート更新→ログ解析」**をノートブック上で再現しています。

※現状の配置は `backup/test.ipynb` です。

### 0. Notebook概要

- LangChain（tool-calling / LangGraph系）+ Deep Agent を使い、テキストから構造（AST）を作り、2文書を比較する流れを検証します。

### 1. `[共通] Agent Log Analyzer`（実行後に使う）

- **目的**: `agent_debug.jsonl` のデバッグログを解析し、`data/runs/【ステータス】日時/` に
  - JSONLログのバックアップ
  - テキストレポート
  - 仮想ファイルシステム上で生成されたファイル（例: 埋めたテンプレート等）  
  を保存します。
- **注意**: このセルは `result2`（比較エージェントの実行結果）が作られた後に実行します。

### 2. `準備`

- 必要パッケージのインストール（コメントで提示）
- `.env` 読み込み（`dotenv.load_dotenv()`）
- `build_llm()` を定義し、OpenAI / Azure OpenAI を環境変数で切り替え
- LangSmith設定（トレーシング無効化など）
- `DebugLoggingMiddleware` を定義し、**ツール呼び出し/サブエージェント実行**をJSONLに記録できるようにします

### 3. `1. カスタムツールの設定`

LLMが“自分で読む/探す”ためのツールを定義します（例）:

- **`read_text_segment`**: 大きいテキストを部分読み
- **`extract_regex_matches`**: 正規表現で候補を抽出（行番号・行テキスト付き）
- **`read_text_file` / `get_file_length`**: 検証用の補助
- **`analyze_visual_contents`**: ドキュメントの特定ページを画像として取得し、LLMで分析（視覚的要素の構造解析に使用）

→ blueprint生成の「見出しパターン探索/誤検知排除」を支えます。

`analyze_visual_contents`は、PDFの特定ページを画像として取得し、プロンプトに従って分析結果を返すツールです。`<!-- VISUAL_CONTENT -->`や`<!-- AGENDA -->`タグが付与されたページの構造を正確に把握するために使用されます。

### 4. `2. 構造化出力（スキーマ）の定義`

- blueprint用: `DocumentStructureBlueprint` / `HierarchyRule` / `ValidationRules` など
- AST用: `DocumentAST` / `DocumentNode`

→ エージェント出力を機械処理しやすい形に固定します。

### 5. `3. エージェントの設定（構造解析=blueprint生成）`

- `create_deep_agent(...)` で **blueprint生成エージェント**を作成
- システムプロンプトで以下を要求:
  - 見出しパターンを探索し、階層レベルと सहजな親子関係を設計
  - `validation_rules` で誤検知を落とす設計
  - 必要ならサブエージェント（validate_blueprint_agent）で検証・修正

### 6. `4. Execution`（blueprint生成の実行）

- `target_file` を読み込み、仮想ファイルシステム（`files`）に投入
- 「見出しパターンを検出してblueprint生成」という依頼でエージェントを実行
- 実行後、メッセージ/ツール呼び出しログを整形して表示

### 7. `5. AST変換`

- `blueprint_ast_builder.build_ast_from_blueprint(...)` で
  - blueprint（正規表現）に沿って見出しを抽出
  - **階層 + 本文（content）**を持つ `*.ast.json` を出力
- `ast_llm_summarizer.summarize_ast_inplace(...)` で
  - **非leafノード**に `content_summary` を付与（leafは空のまま）

### 8. `6. 比較`

#### 8.1 `比較用ツール`

比較用に、以下のようなツール/ロジックを準備します（概念）:

- **`compare_setup`**: AST読み込み→チャンク化→Embedding Index作成→状態保持
- **`compare_all_chunk_similarity_matching`**: 全チャンクの対応候補（top_k）を作る
- **`compare_specified_chunks_diff`**: 高類似度向けのdiff
- **`compare_specified_chunks_llm`**: 低類似度向けのLLM差分抽出

#### 8.2 `実行`（比較の実行）

1) **事前分析エージェント（Pre-Analysis）**

- 2つのASTの関係タイプ（Fix/Revision/…）を推定し、
- 重点観点（例:「変更箇所と影響」）に沿った **具体的手順（plan）** と **テンプレート** を提案します。

2) **比較エージェント（テンプレートを埋める）**

- AST2本、Embeddingキャッシュ、テンプレート（`diff_analysis_template_fujifilm_yuho.md` / リポジトリでは `templates/` 配下）を仮想FSへ投入
- 親エージェント＋複数サブエージェントで、テンプレートを**段階的に編集**しながら比較結果を生成します

3) **追加指示（出力ファイル名の指定）**

- 実行後に「`filled_template.md` として出力して」と追い指示し、最終成果物ファイルを確定させます。

### 9. ログ解析（`analyze_agent_log`）

- `agent_debug.jsonl` を解析し、`data/runs/` にログ・レポート・仮想FS成果物を保存します。

---

## 注意事項（PoC）

- **UI/アップロード/プレビュー画面は未実装**です（現状はローカルファイル＋テンプレート更新の検証が中心）
- LLM/Embeddingを使用するため **実行コスト・実行時間**がかかります
- 長文はコンテキスト制約があるため、ツールで段階的に読む設計になっています

