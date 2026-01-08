# 環境変数一覧（compare_agent / compare_app）

## 方針（`.env` と OS環境変数の優先順位）

- `compare_app` は起動時に `.env` を **補助的に読み込みます**（`python-dotenv`）。
- **OS環境変数が優先**です（`.env` は未設定のキーのみ補完 / `override=False`）。
- `.env` を使わない運用では、`COMPARE_APP_LOAD_DOTENV=0` で無効化できます。
- `main.py`（PoC）は `dotenv.load_dotenv()` を常時実行します（`COMPARE_APP_*` は参照しません）。

---

## アプリ（compare_app）共通

- **COMPARE_APP_DB_PATH**
  - SQLite DBパス
  - default: `repo_root/data/compare_app.db`（起動ディレクトリに依存しない）
- **COMPARE_APP_RUNS_ROOT**
  - Run成果物の保存先ルート（`{RUNS_ROOT}/{run_id}/...`）
  - default: `repo_root/data/runs`（起動ディレクトリに依存しない）
- **COMPARE_APP_LOAD_DOTENV**
  - `.env` 自動読み込みの有効/無効（`0/false/no/off` で無効）
  - default: `1`
- **COMPARE_APP_DOTENV_PATH**
  - 読み込む `.env` のパス（指定が無い場合はリポジトリ直下 `.env`）

---

## LLMプロバイダ共通（src/utils.build_llm, src/ast_llm_summarizer など）

- **LLM_PROVIDER**
  - `openai` / `azure`（`azureopenai`, `azure_openai` も許容）
  - default: `openai`
- **TEMPERATURE**
  - 0推奨（`src/ast_compare` / `src/ast_llm_summarizer` で参照。`src.utils.build_llm` は現状 temperature を渡していない）
- **PDF_LLM_MODEL**
  - `src/pdf_to_text_llm.convert_pdf_with_llm` が使用するモデル（未指定時）
  - 未設定の場合は `AZURE_OPENAI_DEPLOYMENT_NAME_COMPLEX` / `OPENAI_MODEL` 等へフォールバック

---

## OpenAI（Chat + Embedding）

- **OPENAI_API_KEY**（必須）
- **OPENAI_MODEL**
  - デフォルトモデル（例: `gpt-5-mini`）
  - 使われ方:
    - `src/ast_llm_summarizer` は `OPENAI_MODEL` を参照
    - `src/utils.build_llm` はデフォルト `gpt-5-mini`（呼び出し側で `model=` 指定も可能）
    - `src/ast_compare` のフォールバックは `gpt-5.1`
- **OPENAI_EMBEDDING_MODEL**
  - 例: `text-embedding-3-large`
  - `src.tools.compare_setup`（Embedding）で使用

---

## Azure OpenAI（Chat + Embedding）

- **AZURE_OPENAI_ENDPOINT**（必須）
- **AZURE_OPENAI_API_KEY**（必須）
- **AZURE_OPENAI_API_VERSION**（必須）
  - 例: `2024-xx-xx`
- **AZURE_OPENAI_DEPLOYMENT_NAME**
  - チャットモデルのデプロイ名（省略時は `model=` などの指定にフォールバック）
- **AZURE_OPENAI_CHAT_DEPLOYMENT_NAME**
  - 互換用のチャットデプロイ名（`src/ast_compare` が参照）
- **AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME**
  - Embedding用のデプロイ名（例: `text-embedding-3-large`）

補足:
- 一部モジュールでは `AZURE_OPENAI_DEPLOYMENT` / `OPENAI_API_VERSION` も参照します（互換のため）。

---

## （参考）`.env` 例

OpenAI:

```dotenv
LLM_PROVIDER=openai
OPENAI_API_KEY=your_key
OPENAI_MODEL=gpt-5-mini
OPENAI_EMBEDDING_MODEL=text-embedding-3-large
TEMPERATURE=0
```

Azure OpenAI:

```dotenv
LLM_PROVIDER=azure
AZURE_OPENAI_ENDPOINT=https://xxxxx.openai.azure.com/
AZURE_OPENAI_API_KEY=your_key
AZURE_OPENAI_API_VERSION=2024-xx-xx
AZURE_OPENAI_DEPLOYMENT_NAME=gpt-5-mini
AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME=text-embedding-3-large
TEMPERATURE=0
```
