# テスト実施ガイド

## 概要

このドキュメントは `document_process_agent` プロジェクトのCLIベースのテスト実施方法を説明します。

テストは `pytest` を使用し、`document_process_app.cli` モジュールの各コマンドを検証します。

## 前提条件

### 1. Python環境

- Python 3.10以上を推奨
- 仮想環境の使用を推奨

### 2. 依存パッケージのインストール

```powershell
# プロジェクトルートで実行
pip install -r requirements.txt
```

### 3. 環境変数（.env）

`.env` ファイルに以下の設定が必要です（realモードでLLMを使う場合）:

```env
OPENAI_API_KEY=your_openai_api_key
AZURE_OPENAI_API_KEY=your_azure_api_key  # Azureを使う場合
# ... その他の設定
```

> **Note:** dummyモードのテストはAPIキー不要で実行可能です。

## テストの実行方法

### 基本的な実行

```powershell
# プロジェクトルートで実行
python -m pytest tests/ -v
```

### タイムアウト付き実行（推奨）

```powershell
python -m pytest tests/ -v --timeout=60
```

### 特定のテストクラスのみ実行

```powershell
# Createコマンドのテストのみ
python -m pytest tests/test_cli.py::TestCliCreate -v

# Executeコマンドのテストのみ
python -m pytest tests/test_cli.py::TestCliExecute -v

# エンドツーエンドテストのみ
python -m pytest tests/test_cli.py::TestCliEndToEnd -v
```

### 特定のテストメソッドのみ実行

```powershell
python -m pytest tests/test_cli.py::TestCliExecute::test_execute_dummy_mode_succeeds -v
```

### 詳細な出力付き実行

```powershell
python -m pytest tests/ -v -s  # print文の出力を表示
```

## テスト構成

### ディレクトリ構造

```
tests/
├── __init__.py           # パッケージ初期化
├── conftest.py           # pytest設定・共通fixtures
├── fixtures/             # テスト用データ
│   ├── sample_doc_a.txt  # サンプル文書A
│   └── sample_doc_b.txt  # サンプル文書B
├── test_cli.py           # CLIコマンドテスト
└── README.md             # このドキュメント
```

### テストクラス一覧

| クラス名 | 説明 |
|---------|------|
| `TestCliCreate` | `create` コマンドのテスト（Run作成） |
| `TestCliExecute` | `execute` コマンドのテスト（同期実行） |
| `TestCliList` | `list` コマンドのテスト（Run一覧） |
| `TestCliArtifacts` | `artifacts` コマンドのテスト（成果物一覧） |
| `TestCliExport` | `export` コマンドのテスト（成果物エクスポート） |
| `TestCliEndToEnd` | エンドツーエンドテスト（全フロー） |
| `TestCliErrors` | エラーケースのテスト |

### テストケース詳細

#### TestCliCreate
- `test_create_run_dummy_mode`: dummyモードでRunを作成できる
- `test_create_run_with_existing_test_data`: 既存の軽量テストデータでRunを作成できる

#### TestCliExecute
- `test_execute_dummy_mode_succeeds`: dummyモードで同期実行が成功する
- `test_execute_creates_artifacts`: 実行後に成果物が生成される

#### TestCliList
- `test_list_runs`: Run一覧が取得できる

#### TestCliArtifacts
- `test_artifacts_after_execute`: 実行後の成果物一覧が取得できる

#### TestCliExport
- `test_export_template_filled`: template_filledをエクスポートできる

#### TestCliEndToEnd
- `test_full_dummy_workflow`: create→execute→artifacts→exportの全フローが動作する

#### TestCliErrors
- `test_execute_nonexistent_run`: 存在しないrun_idでエラーになる
- `test_export_nonexistent_kind`: 存在しないファイルでエラーになる

## テストデータ

### 自動生成されるテストデータ

- `tests/fixtures/sample_doc_a.txt`: 簡易サンプル文書A
- `tests/fixtures/sample_doc_b.txt`: 簡易サンプル文書B（Aの改訂版）

### 既存の軽量テストデータ

- `data/input/test_small_rules_v1.txt`: アクセス制御規程 v1
- `data/input/test_small_rules_v2.txt`: アクセス制御規程 v2

## 実行結果の例

```
============================= test session starts =============================
platform win32 -- Python 3.13.1, pytest-8.4.2
plugins: timeout-2.4.0
collected 10 items

tests/test_cli.py::TestCliCreate::test_create_run_dummy_mode PASSED      [ 10%]
tests/test_cli.py::TestCliCreate::test_create_run_with_existing_test_data PASSED [ 20%]
tests/test_cli.py::TestCliExecute::test_execute_dummy_mode_succeeds PASSED [ 30%]
tests/test_cli.py::TestCliExecute::test_execute_creates_artifacts PASSED [ 40%]
tests/test_cli.py::TestCliList::test_list_runs PASSED                    [ 50%]
tests/test_cli.py::TestCliArtifacts::test_artifacts_after_execute PASSED [ 60%]
tests/test_cli.py::TestCliExport::test_export_template_filled PASSED     [ 70%]
tests/test_cli.py::TestCliEndToEnd::test_full_dummy_workflow PASSED      [ 80%]
tests/test_cli.py::TestCliErrors::test_execute_nonexistent_run PASSED    [ 90%]
tests/test_cli.py::TestCliErrors::test_export_nonexistent_kind PASSED    [100%]

======================= 10 passed in 187.33s (0:03:07) ========================
```

## CI/CD統合

### GitHub Actions（例）

```yaml
name: CLI Tests

on: [push, pull_request]

jobs:
  test:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - name: Install dependencies
        run: pip install -r requirements.txt
      - name: Run tests
        run: python -m pytest tests/ -v --timeout=120
```

## トラブルシューティング

### Q: テストが長時間かかる

A: `--timeout` オプションでタイムアウトを設定してください。dummyモードのテストは通常1-3分で完了します。

### Q: ImportError が発生する

A: プロジェクトルートで実行しているか確認してください。`conftest.py` が `sys.path` を設定しています。

### Q: データベースエラーが発生する

A: `data/document_process_app.db` が破損している可能性があります。削除して再実行してください。

```powershell
Remove-Item data/document_process_app.db
python -m pytest tests/ -v
```

### Q: realモードでテストしたい

A: `.env` に有効なAPIキーを設定し、以下のようにテストを作成してください:

```python
def test_real_mode(sample_doc_a, sample_doc_b):
    # --mode real を指定
    result = subprocess.run([
        sys.executable, "-m", "document_process_app.cli",
        "create", "--doc", str(sample_doc_a), "--doc", str(sample_doc_b),
        "--mode", "real",
    ], ...)
```

> **Warning:** realモードはLLM APIを呼び出すため、コストが発生し、時間がかかります。

## 関連ドキュメント

- `design/fastapi_htmx_app_requirements.md`: アプリ要件
- `design/run_executor_pipeline_api.md`: パイプラインAPI仕様
- `design/roadmap_fastapi_htmx_cli.md`: ロードマップ
