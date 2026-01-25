## document_process_agent

`document_process_app/` は **FastAPI+HTMX の Web UI** と **CLI** を備えた、ドキュメント処理（PDF/TXT→構造化→タスク実行）用のMVPです。

- **入力**: `documents`（1件以上）
- **成果物**: `data/runs/{run_id}/...` として保存（テンプレ、ログ、イベント等）

---

## セットアップ（PowerShell想定）

### 1) 仮想環境

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
```

### 2) 依存関係（例）

基本は `requirements.txt` を使ってインストールしてください（`document_process_app/` とテストも含めた想定）。

```powershell
pip install -r requirements.txt
```

補足:
- `deepagents` はバージョン差分でAPIが変わることがあります。importエラーが出る場合は、まず `requirements.txt` に合わせて入れ直してください。

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

Gemini (AI Studio) 利用例:

```dotenv
LLM_PROVIDER=gemini
GOOGLE_API_KEY=your_key
GEMINI_MODEL=gemini-2.0-flash
GEMINI_EMBEDDING_MODEL=text-embedding-004
TEMPERATURE=0
```

#### 429 / insufficient_quota について（重要）

OpenAI利用時に `RateLimitError: 429 (insufficient_quota)` が出る場合、**APIキーのクォータ/請求設定**が原因です。

- 対処:
  - OpenAIの請求/利用上限を確認する
  - もしくは `.env` を Azure OpenAI（`LLM_PROVIDER=azure`）に切り替える

環境変数の一覧は `design/environment_variables.md` を参照してください（`.env` は任意、OS環境変数が優先）。

---

## Web UI / CLI

`document_process_app/` は **FastAPI+HTMX UI** と **CLI** の土台です。

- dummyモード: 「Run作成→実行→イベント追跡→テンプレ生成（ダミー）」まで動作
- realモード: 「txt→blueprint→AST→pre_analysis→execute_analysis→filled（最終成果物）」まで接続
  - 必要ペアの比較準備は `pair_compare_setup(doc_a_id, doc_b_id, purpose)` を execute_analysis 内で実行
  - 本文検索は `search_by_keyphrase(file_path, phrase, intent)` で **単一AST** を対象に実行（ペア非依存）

依存関係:

```powershell
pip install -r requirements.txt
```

※ `document_process_app/` は起動時に `.env` を自動で読み込みます（UI/CLI共通）。

### UI / API / CLI の違い（入口）と同一性（内部処理）

このアプリは「実行方法」が3つありますが、**入口が違うだけで、内部では同じ処理（Run作成→開始→イベント/成果物記録）**を呼びます。

- **UI（Web）**: ブラウザから `POST /admin/runs` 等へアクセス（フォーム/HTMX向け）
- **API（JSON）**: プログラムから `POST /api/runs` 等を叩く（JSON入出力）
- **CLI（コマンド）**: `python -m document_process_app.cli ...`（テスト/自動化向け）

重要:
- UIから実行しても **CLIプロセスを起動するわけではありません**。ブラウザ→FastAPIへHTTPリクエストが飛び、サーバ内で実行が始まります。
- UI用エンドポイント（`/admin/...`）とAPI用（`/api/...`）は **別のURL** ですが、内部では共通のサービス層（`RunService`）→実行エンジン（`RunExecutor`）に集約されています。

| 実行方法 | 何に向いているか | 入口の例 | 返り値/見え方 |
| --- | --- | --- | --- |
| UI（Web） | 手動で試す・進捗を見る | `GET /admin` / `POST /admin/runs` | 画面（SSEでイベントが増える） |
| API（JSON） | 連携・スクリプト化 | `POST /api/runs` | JSON（run_idなど） |
| CLI | 回帰テスト・自動実行 | `python -m document_process_app.cli ...` | JSONを標準出力（exit codeで成否） |

### CLI（同期実行・テスト向け）

```powershell
# Run作成（推奨: documents を複数指定。合計1件以上）
python -m document_process_app.cli create --doc .\data\input\test_small_rules_v1.txt --doc .\data\input\test_small_rules_v2.txt --request "差分を要約して" --mode real

# Run実行（run_idを指定）
python -m document_process_app.cli execute <run_id>

# イベント追跡
python -m document_process_app.cli tail <run_id>
```

realモード（本番処理）:

```powershell
python -m document_process_app.cli create --doc .\data\input\仕訳定義書.txt --doc .\data\input\仕訳定義書_文体変更版.txt --mode real
python -m document_process_app.cli execute <run_id>
```

コスト節約のスモーク（小規模テスト文書 + gpt-5-mini）:

```powershell
python -m document_process_app.cli create --doc .\data\input\test_small_rules_v1.txt --doc .\data\input\test_small_rules_v2.txt --mode real --params '{\"llm_complex_model\":\"gpt-5-mini\",\"summarize_ast\":false}'
python -m document_process_app.cli execute <run_id>
```

#### ステップの実行範囲を絞る（UI/CLI共通）

`create` 時の `params` で以下を指定すると、パイプラインのステップ実行を絞り込めます（未指定なら全ステップ実行）。

- `steps_include`: 実行したいステップ名の配列（例: `["build_blueprint_all", "execute_analysis"]`）
- `step_from`: 開始ステップ名（例: `build_blueprint_all`）
- `step_to`: 終了ステップ名（例: `execute_analysis`）

範囲指定は `document_process_app/bootstrap.py` に並んだステップ順が基準です。`steps_include` と `step_from`/`step_to` は併用でき、両方の条件に合致したステップのみ実行されます。

```powershell
# 例: AST生成以降だけ実行
python -m document_process_app.cli create --doc .\data\input\仕訳定義書.txt --doc .\data\input\仕訳定義書_文体変更版.txt --mode real --params '{\"step_from\":\"build_ast_all\",\"step_to\":\"execute_analysis\"}'
python -m document_process_app.cli execute <run_id>
```

### 成果物（Artifacts）の確認方法

- **Web UI**: Run詳細（`/admin/runs/{run_id}`）の「成果物」から `view` / `download`
- **CLI**:
  - 一覧: `python -m document_process_app.cli artifacts <run_id>`
  - 取得: `python -m document_process_app.cli export <run_id> --kind template_filled --out .\\template_filled.md`
  - 取得（documentsの例）: `python -m document_process_app.cli export <run_id> --kind blueprint_d1 --out .\\blueprint_d1.json`
- **ファイル直接**: `data/runs/{run_id}/work/` / `out/` / `log/` 配下を開く

### Web UI（FastAPI）

```powershell
uvicorn document_process_app.web.app:app --reload
```

ブラウザで `http://127.0.0.1:8000/admin` を開いてください。
（ポートを変えて起動している場合は例: `http://127.0.0.1:8001/admin`）

主な画面:
- Run一覧: `/admin`
- Run作成: `/admin/runs/new`（複数ドキュメント選択/アップロード）
- ドキュメント一覧: `/admin/documents`

---

## 参考ドキュメント

- `design/` 配下の設計メモ（環境変数など）を参照してください。
