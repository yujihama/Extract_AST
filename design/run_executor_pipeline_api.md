## UI/CLI共通の入口API（RunExecutor / Pipeline I/F）

### 目的

- **UI（FastAPI/HTMX）とCLIが同じ実行入口を使う**ことで、仕様の二重実装を避ける。
- back-end実行基盤（in-process → Celery）を差し替えても、UI/CLIの呼び方を変えない。

---

## 実装状況（最新）

- 実装本体は `compare_app/` 配下（UI/CLI共通）
  - `Pipeline`: `compare_app/core/pipeline.py`
  - `RunExecutor`: `compare_app/core/run_executor.py`
  - `InProcessJobQueue`: `compare_app/infra/inmemory.py`
  - `SqliteRunRepository` / `SqliteEventSink`: `compare_app/infra/sqlite_store.py`
- `src.tools.COMPARE_STATE` は `compare_app/compat/patch_src_tools.py` によりスレッドローカルへ差し替え
- `Pipeline` は条件付きstep（`ConditionalStep`）により `step_skipped` をemit可能
- キャンセルは `CancellationRegistry` による協調的キャンセル（`request_cancel(run_id)`）

---

## レイヤ構造（推奨）

- **Core（純粋な実行ロジック）**
  - `Pipeline`: step列を順に実行し、stepイベントを必ず出す
  - `RunExecutor`: runの状態更新・実行開始・キャンセル・ジョブ投入をまとめる“入口”
- **Infra（外部I/O）**
  - `RunRepository`（SQLite想定）: run/status/paramsを保存
  - `EventSink`（SQLite想定）: UIで追跡するイベントを保存
  - `ArtifactStore`（FS想定）: 入力/成果物の配置・パス解決
  - `JobQueue`（in-process→Celery）: バックグラウンド実行の抽象

UI/CLIは **Coreの `RunExecutor`** のみを呼ぶ（InfraはDIで注入）。

---

## 1) Domain Model（最小）

- `RunStatus`: `queued | running | succeeded | failed | cancelled | waiting_user(予約)`
- `RunRecord`: `run_id, status, created_at, started_at, finished_at, params_json, error_message, workdir`
- `ArtifactKind`: `input_doc_a, input_doc_b, txt_a, txt_b, blueprint_a, blueprint_b, ast_a, ast_b, template_draft, template_filled, log_jsonl, ...`
- `ArtifactRecord`: `artifact_id, run_id, kind, path, created_at, updated_at, meta_json`
- `RunEvent`: `event_id, run_id, ts, event_type, payload_json`

---

## 2) Protocol（抽象I/F）

### EventSink

- `emit(run_id, event_type, payload) -> None`
- `list(run_id, after_event_id=None, limit=...) -> list[RunEvent]`

### RunRepository

- `create_run(run: RunRecord) -> None`
- `get_run(run_id) -> RunRecord`
- `update_status(run_id, status, *, message=None) -> None`
- `set_error(run_id, error_message) -> None`
- `list_runs(limit=..., offset=...) -> list[RunRecord]`（UI一覧用）

### ArtifactStore / ArtifactRepository（最小）

- `ensure_run_dirs(run_id) -> RunPaths`
- `add_artifact(run_id, kind, src_path | content) -> ArtifactRecord`
- `get_artifact_path(run_id, kind | artifact_id) -> str`

### JobQueue（in-process→Celery）

- `enqueue(run_id, job_type, payload) -> job_id`
- `cancel(run_id) -> bool`

---

## 3) Pipeline I/F

### RunContext（stepが受け取る共通コンテキスト）

- `run_id`
- `params`
- `paths`（run_dir/input/work/out/log/cache） ※MVPは辞書
- `events`（EventSink）
- `cancellation`（CancellationToken）

### Step I/F

- `name: str`
- `run(ctx) -> None`

### Pipeline.run(ctx)

必須挙動:

- 各stepの前後で必ず `step_started/step_finished/step_failed` をemit
- 条件付きstepの場合、走らないとき `step_skipped` をemit
- stepが `should_run(ctx)` を実装している場合は、出力済み成果物があればスキップできる（idempotency）
  - 例: `force=true` を params に渡すと再実行（上書き）できる設計にする
- 例外時:
  - `step_failed` をemit
  - 例外を上位へ伝播（RunExecutorが `failed` に遷移させる）

---

## 4) RunExecutor I/F（UI/CLI共通入口）

### create_run

- 入力（docA/docBのパス、params）から `run_id` を作り、FS/DBを初期化
- `artifact(input_doc_a/input_doc_b)` を登録
- 返り値: `RunRecord`

### start（UI向け）

- `queued` に遷移し、`JobQueue.enqueue(...)` でバックグラウンド実行

### execute（CLI/テスト向け）

- 現在プロセスで同期的に pipeline を実行
- 終了コード化しやすいように、成功/失敗/キャンセルを区別して例外 or Resultで返す

### cancel

- `request_cancel(run_id)` により `CancellationRegistry` にキャンセル要求を登録（協調的キャンセル）

---

## 5) “拡張しにくさ”回避のポイント（I/Fに反映）

- **waiting_user**: stateモデルに予約（将来の人手介入）
- **idempotent step**: stepは「入力artifact→出力artifact」に寄せ、`force=false` でスキップ可能にする
- **payload肥大化回避**: EventSinkは「要約＋参照」に寄せ、本文はartifactで保持
- **JobQueue差し替え**: UI/CLIは enqueue 実装を意識しない

