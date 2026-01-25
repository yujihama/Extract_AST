# FastAPI + HTMX 実装機能整理（MVP→拡張）

## このドキュメントの目的

実装前に「**HTMX+FastAPIで何を作るか**」を、以下に分解して合意するためのチェックリストです。

- 画面（ページ）と共通パーツ
- エンドポイント（HTML/フラグメント/SSE/ダウンロード）
- Run（実行）とイベント（deep_agent / tool / subagent）の可視化
- バックグラウンド実行（まず非Celery、後でCeleryへ移行しやすい構造）
- 永続化（FS: 入力/成果物、SQLite: 履歴/イベント/メタ）

---

## 実装状況（最新）

### 実装済み（MVP土台）

- **共通入口（UI/CLI）**: `document_process_app/core/run_executor.py`, `document_process_app/core/pipeline.py`
  - `RunExecutor.create_run/start/execute/request_cancel`
  - `Pipeline` は `step_started/finished/failed` に加え、条件により `step_skipped` をemit
  - `mode=dummy|real` で経路分岐（UI/CLIから選択可能）
- **非Celery実行**: `document_process_app/infra/inmemory.py`
  - `InProcessJobQueue`（別スレッド）で `run_pipeline` を起動
- **SQLiteイベント/Run保存**: `document_process_app/infra/sqlite_store.py`
  - DB: `data/document_process_app.db`（`schema_version`, `runs`, `run_events`, `artifacts`）
- **Web UI（FastAPI + HTMX）**: `document_process_app/web/app.py`
  - `GET /admin`（Run一覧）
  - `GET /admin/runs/new`（Run作成）
  - `POST /admin/runs`（upload→Run作成、任意でstart）
  - `GET /admin/runs/{run_id}`（Run詳細）
  - `POST /admin/runs/{run_id}/start`, `POST /admin/runs/{run_id}/cancel`
  - `GET /admin/runs/{run_id}/events`（SSE）
  - `GET /admin/runs/{run_id}/template/{draft|filled}`（テンプレプレビュー）
- **JSON API（UI未実装でも使えるI/F）**: `document_process_app/web/app.py`
  - `GET /api/runs`, `GET /api/runs/{run_id}`
  - `POST /api/runs`（テキスト/ファイル指定でRun作成）
  - `POST /api/runs/{run_id}/start`, `POST /api/runs/{run_id}/cancel`
  - `GET /api/runs/{run_id}/events`（JSONイベント一覧）
  - `GET /api/runs/{run_id}/blueprint/{doc_id}` / `PUT /api/runs/{run_id}/blueprint/{doc_id}`
- `GET /api/runs/{run_id}/blueprint/{doc_id}/preview`
- `GET /api/runs/{run_id}/blueprint/{doc_id}/validate`
- `GET /api/runs/{run_id}/ast/{doc_id}`
  - `GET /api/runs/{run_id}/compare/initial_matching`
- **CLI（テスト自動化の入口）**: `document_process_app/cli.py`
  - `create/start/execute/cancel/tail/list/artifacts/export`
- **エージェント可視化（案A）**:
  - `document_process_app/agents/middleware.py` の `EventSinkMiddleware` が `agent_*` / `tool_call_*` を `EventSink` にemit

### 実装済み（dummyモード）

- `data/runs/{run_id}/work/template_draft.md` と `out/template_filled.md` を生成（動作確認用）

### 一部実装（realモードの骨格）

- `PDF/TXT → txt → blueprint（LLM） → AST（非LLM）` までのステップを追加（`document_process_app/core/real_steps.py`）
  - blueprint生成にはLLMキーが必要（未設定時はエラー）
- `pre_analysis → execute_analysis` のステップを追加（`document_process_app/core/compare_steps.py`）
  - 比較準備（`pair_compare_setup`）は embedding/LLMキー必須（未設定時は失敗）
  - `pre_analysis` / `execute_analysis` はキー未設定時フォールバックでテンプレ生成は可能（内容は簡易）

### 検証済み（real本番接続）

- TXT入力で end-to-end（txt→blueprint→AST→pre_analysis→execute_analysis→filled）が `succeeded` まで完走することを確認済み
- PDF入力は fast変換で end-to-end が `succeeded` まで完走することを確認済み（llm変換は未検証）

### 実装済み（軽量モード / Lightweight Mode）

入力規模やタスク内容が軽量な場合、`pre_analysis` ステップだけで最終成果物まで作り、後続の `execute_analysis` は **実質no-op** になる機能。

- **判定**: `work/pre_analysis.json` の `is_complete`（bool）
- **動作**:
  - `is_complete=True` の場合、`pre_analysis` が `out/template_filled.md` を直接出力する
  - `execute_analysis` は `pre_analysis.json` を参照し、`is_complete=True` のときは `out/template_filled.md` の存在を検証して終了する
- **備考**:
  - `request_text` と `documents`（doc_id/paths など）を前提に、LLMが実行計画と出力粒度を判断する

### 残課題（MVP後の優先度高）

- PDF→TXT（LLM）の実測検証（ページ範囲/コスト/品質、UIパラメータ調整）
- 長時間stepの途中キャンセル（`execute_analysis` / `summarize_ast` など）
- artifacts登録の網羅性（log/cacheなどの登録漏れ解消、kind体系の整理）

---

## 0. スコープ定義（MVP）

### 0.1 MVPで「必ず」できること

- PDF/TXTを **1件以上** アップロード/選択してRunを作成
- 必要に応じてPDF→TXT変換（高速/LLM）
- blueprint生成（LLM）→検証→（任意で手動編集）→AST生成
- Pre-Analysisで **タスク計画（execution_plan）＋テンプレ生成**
- 比較分析で **テンプレを段階的に更新して埋める**
- 実行過程（subagent/tool呼び出し）を **UIで追跡**（タイムライン＋詳細）
- filled template をUIで閲覧/ダウンロード

### 0.2 MVPでの制約（最初は割り切る）

- 認証なし、単一ユーザー
- 同時実行は **最大1 Run**（または「run_id単位排他」）で開始してOK（決定）  
  ※ただし、設計はrun_id分離前提（後でCelery/並列へ）

### 0.3 設計の前提として固める論点（拡張しにくさを避ける）

MVPでは簡略化しても良いが、**最初に前提として決めておくと後で破綻しにくい**論点です。

- **Runの状態モデル（ステートマシン）**
  - 推奨: `queued/running/succeeded/failed/cancelled` に加えて、将来の人手介入用に `waiting_user`（保留）を予約しておく
  - 理由: blueprint/テンプレを「ユーザー編集→再開」する拡張が自然にできる

- **ステップの粒度と再実行（idempotency）**
  - 推奨: すべての処理を `step` に分割し、各 step は「入力artifact→出力artifact」を作る純関数的な責務に寄せる
  - 推奨: `force=false` なら既存出力があればスキップできる（再開/リトライに強い）
  - 現状: `force=true` を params に渡すと、実装済みstep（txt/blueprint/ast/template系）は再生成できる

- **成果物（artifact）を“型”として扱う**
  - 推奨: `kind`（input_txt/blueprint/ast/template_draft/template_filled/log…）を固定し、UIとAPIは `kind` に依存して表示する
  - 理由: 画面/処理を増やしても「新しいartifact種別を追加」するだけで拡張できる

- **テンプレ更新の版管理（差し戻し/監査）**
  - 推奨: `template_filled.md` は“最新”として上書きしつつ、必要に応じて `template_filled.v{n}.md` のスナップショットを残せる構造にする
  - 理由: 「どの段階で何が追記されたか」を後から検証できる（監査・デバッグ）

- **イベント設計（UI可視化の根幹）**
  - 決定: **案A**（`DebugLoggingMiddleware`→`EventSink.emit`）を採用
  - 推奨: event payload は「要約＋参照（file/offset等）」に寄せ、巨大な本文はartifactに逃がす（DB肥大化回避）
  - 推奨: correlation id（`invoke_id`/`tool_call_id`/`parent_invoke_id`）を保存し、親子関係をUIで復元できるようにする

- **キャンセルの契約**
  - 推奨: “協調的キャンセル”として、step境界と長時間ループ（ページ処理/バッチ）で `cancel_requested` をチェックする
  - 理由: Celery移行後も同じ契約で止めやすい

- **入出力パスの正規化（CWD依存を消す）**
  - 推奨: すべての読み書きは `run_dir` 基準で行い、既存コードの `data/input` 固定参照は段階的に剥がす
  - 理由: 並列実行・ワーカー分離・コンテナ化で詰まりやすい箇所

- **SQLiteのマイグレーション方針**
  - 推奨: 初期から「schema_version」を持ち、将来 Alembic 等に移行できる前提で作る（テーブル追加が頻発するため）

- **Embeddingキャッシュの責務分割**
  - 推奨: 当面は既存 JSON キャッシュを継続しつつ、DBには「どのembedding_id/キャッシュパスを使ったか」だけを記録
  - 理由: 巨大化しやすいデータをDBに入れ過ぎない（後で必要になったら移行）

---

## 1. UI（ページ）一覧（HTMX前提）

### 1.1 主要ページ

- **Run一覧**: `GET /admin`
  - run_id / status / created_at / doc名 / 直近更新
  - 操作: 新規作成、詳細へ

- **Run作成（ウィザードでも単ページでも可）**: `GET /admin/runs/new`
  - ドキュメントを複数選択またはアップロード（PDF/TXT、合計1件以上）
  - PDFの場合の変換設定
  - 実行開始ボタン（Run作成＋startを一括 or まず作成して後でstart）

- **Run詳細（監視/閲覧の中心）**: `GET /admin/runs/{run_id}`
  - status / progress / current_step
  - 成果物一覧（input/work/out/log/cache）
  - イベントタイムライン（リアルタイム）
  - 生成テンプレ（draft）と filled template のプレビュー
  - 操作: キャンセル、再読み込み（HTMXで部分更新）

### 1.2 Run詳細の部分更新（HTMX “fragment”）

Run詳細は「常時更新される領域」と「手動で開く領域」を分けると実装しやすいです。

- **statusカード**（数秒ポーリング or SSEトリガでhx-get）  
  - `GET /admin/runs/{run_id}/partials/status`
- **成果物一覧**（更新トリガ: artifact追加/更新イベント）  
  - `GET /admin/runs/{run_id}/partials/artifacts`
- **テンプレプレビュー**（生成/更新イベントで再描画）  
  - `GET /admin/runs/{run_id}/partials/template?kind=draft|filled`
- **イベントタイムライン**（SSEで追記）  
  - `GET /admin/runs/{run_id}/events`（SSE）
  - HTMLポーリング用の `partials/events` は現状未実装

---

## 2. エンドポイント一覧（MVP）

### 2.1 HTMLページ

- `GET /admin`
- `GET /admin/runs/new`
- `GET /admin/runs/{run_id}`

### 2.2 HTMXフラグメント（HTML）

- `GET /admin/runs/{run_id}/partials/status`
- `GET /admin/runs/{run_id}/partials/artifacts`（実装済み: DB優先＋FS補完）
- `GET /admin/runs/{run_id}/partials/template?kind=draft|filled`（実装済み）

### 2.3 コマンド（POST）

- `POST /admin/runs`  
  - フォーム送信: ドキュメント（複数）＋変換設定を受けてrun作成
  - レスポンス: `303 See Other` で `/admin/runs/{run_id}` へ

- `POST /admin/runs/{run_id}/start`  
  - Runを `queued`→`running` にし、バックグラウンド処理を起動
  - レスポンス: HTMXなら statusフラグメントを返す

- `POST /admin/runs/{run_id}/cancel`  
  - MVPは「キャンセル要求を記録して以後のステップ開始を止める」でも可

### 2.4 SSE（リアルタイム）

- `GET /admin/runs/{run_id}/events`（`text/event-stream`）
  - UIは「新規イベントを追記」する
  - HTMX SSE拡張を使うか、素のEventSourceでもOK

### 2.5 成果物の閲覧/ダウンロード

- `GET /admin/runs/{run_id}/partials/artifacts`（一覧HTML, DB優先＋FS補完）
- `GET /admin/runs/{run_id}/artifacts/view/{rel_path}`（テキストプレビュー）
- `GET /admin/runs/{run_id}/artifacts/download/{rel_path}`（ダウンロード）

### 2.6 JSON API（UI未実装でも利用可能）

UIで未実装の機能（例: AST検索/blueprint検証/テキスト貼り付け入力）も、先にAPIだけ提供しておく。

- `GET /api/runs` / `GET /api/runs/{run_id}`
- `POST /api/runs`（テキスト/ファイル指定でRun作成）
- `POST /api/runs/{run_id}/start` / `POST /api/runs/{run_id}/cancel`
- `GET /api/runs/{run_id}/events`（SSEの代替: JSON取得）
- `GET /api/runs/{run_id}/blueprint/{doc_id}` / `PUT /api/runs/{run_id}/blueprint/{doc_id}`
- `GET /api/runs/{run_id}/blueprint/{doc_id}/preview`
- `GET /api/runs/{run_id}/blueprint/{doc_id}/validate`
- `GET /api/runs/{run_id}/ast/{doc_id}`（mode=summary|outline|chunk|search）
- `GET /api/runs/{run_id}/compare/initial_matching`

---

## 3. Run作成フォーム（入力項目）案

### 3.1 必須

- documents: file（PDF or TXT）または既存ドキュメント選択（**合計1件以上**）

### 3.2 PDF変換設定

- mode: `fast`（従来） / `llm`（現行UIは **全ドキュメント共通**）
- start_page / end_page（任意）
- batch_size（llm時）
- use_image（llm時）

### 3.3 比較/LLM設定（MVPではデフォルトでもOK）

- embedding cache: 共有 or run専用（まずは既存JSONキャッシュのパスを利用）
- マッチング: top_k, alpha, beta, min_score
- LLMモデル（必要なら）
  - `llm_complex_model`（比較/blueprintなどの"複雑系"に使うモデル）
  - `summarize_ast` / `ast_summary_model`（AST枝サマリ付与）

### 3.4 重点比較観点（comparison_focus）

- `comparison_focus`: 重点比較観点のリスト（任意）
  - 例: `["ルールの追加・削除", "条件式の変更", "例外処理の変更"]`
  - 指定すると`pre_analysis`のプロンプトに注入され、LLMがその観点を重視して分析
  - 将来的にUI上でユーザーが入力することを想定

### 3.5 成果物流用（reuse_artifacts_from）

- `reuse_artifacts_from`: 既存RunのID（任意）
  - 指定すると、以下の成果物をコピーして再利用:
    - `work/ast_<doc_id>.ast.json`
    - `work/blueprint_<doc_id>.json`
  - ユースケース: AST作成済みのRunを流用して、`pre_analysis`のみ再実行
  - UI: （将来）Run作成画面で「成果物流用」セクションから選択可能（succeededのRunのみ表示）
  - イベント: `artifacts_reused`または`artifacts_reuse_failed`がemitされる

### 3.6 入力ファイル流用（use_files_from_run）

- `use_files_from_run`: 既存RunのID（任意）
  - 指定すると、そのRunの入力ファイルを再利用
  - 新規ファイルアップロードが不要になる
  - 状況: 現状の実装では **未対応**（将来要件）
  - UI: （将来）Run作成画面で「過去のRunから入力ファイルを選択」ドロップダウン
    - 各Runのファイル名も表示: `run_id... | ファイルA / ファイルB (日時)`
  - ユースケース: 同じドキュメントで異なるパラメータで再実行

### 3.7 ステップ選択（step filtering）

- `step_from`: 開始ステップ名（空欄で最初から）
- `step_to`: 終了ステップ名（空欄で最後まで）
- **UI対応済み**: Run作成画面のドロップダウンで選択可能
- 利用可能なステップ名:
  - **realモード**: `ensure_text_all`, `build_blueprint_all`, `build_ast_all`, `summarize_ast_all`, `pre_analysis`, `execute_analysis`
  - **dummyモード**: `dummy_prepare`, `dummy_agent_trace`, `dummy_write_template_draft`, `dummy_analyze`, `dummy_fill_template`

---

## 4. バックグラウンド実行（非Celery→Celery移行容易）

### 4.1 抽象インターフェース（必須）

- `JobQueue.enqueue(run_id, job_type, payload) -> job_id`
- `JobQueue.cancel(run_id) -> bool`
- `EventSink.emit(run_id, event_type, payload)`

MVPは `InProcessJobQueue` で実装し、Celery導入時は `CeleryJobQueue` に差し替える。

### 4.2 パイプライン（推奨ステップ）

Runの処理は「止めやすく、可視化しやすい」粒度で分割する。

1. **ensure_text_all**
   - 全ドキュメントのPDF→TXT（必要な場合）と正規化
2. **build_blueprint_all**
   - 各ドキュメントの blueprint 生成（LLM）
3. **build_ast_all**
   - 各ドキュメントの AST 生成（非LLM）
4. **summarize_ast_all（任意）**
   - AST枝サマリ（LLM、`summarize_ast=true` のとき）
5. **pre_analysis（重要）**
   - 依頼文＋documentsから、実行計画（execution_plan）とテンプレ（template_draft）を生成
6. **execute_analysis（重要）**
   - pre_analysis の計画に従い、必要ペアは `pair_compare_setup` で準備しつつテンプレを埋めて `template_filled.md` を作る

補足:
- 比較準備は `pair_compare_setup` に統一する
- `compare_*` 系ツールは `doc_a_id/doc_b_id` を必須とし、ペアごとに状態を復元して動作する
- 本文検索は `search_by_keyphrase(file_path, phrase, intent)` で **単一AST** を対象に実行する（ペア非依存）

### 4.3 ステップ選択パラメータ（step filtering）

`params` で以下を指定すると、実行するステップを絞り込める（部分再実行やデバッグに有用）:

- `steps_include`: 実行するステップ名のリスト（例: `["build_blueprint_all", "execute_analysis"]`）
- `step_from` / `step_to`: 範囲指定（開始〜終了ステップ名）

未指定なら全ステップを対象とする。詳細は `design/run_executor_pipeline_api.md` を参照。

---

## 5. イベント可視化（deep_agent / tool / subagent）

### 5.1 UIに表示したい最小イベント

- `run_status_changed`: queued/running/succeeded/failed/cancelled
- `step_started` / `step_finished` / `step_failed` / `step_skipped`
- `agent_start` / `agent_end`
- `tool_call_start` / `tool_call_result` / `tool_call_error`
- `artifact_created` / `artifact_updated`

※ 現状のUI（`run_detail.html`）は上記イベントをSSEで受け取り、タイムラインに追記表示する。

### 5.2 イベントのソース（どこから取るか）

MVPで現実的な案は2つ:

#### 案A: エージェント実行を middleware で EventSink に流す（採用）

- 現状は `document_process_app/agents/middleware.py` の `EventSinkMiddleware` を deep_agent/subagent に付与し、
  `agent_start/end` および `tool_call_*` を `EventSink.emit(...)` する。
- 利点: リアルタイム、構造化、tail不要

#### 案B: 既存JSONLを runごとに出力し、tail→SQLiteへ取り込む（改修少）

- まず「run_idごとにログファイル名/保存先」を分離する
- 取り込みスレッドが JSONL の新規行を読み、`run_events` に保存
- 利点: middleware改修が最小、既存資産を活かせる

MVPでは案Bでも良いが、Celery移行・リアルタイム性の観点では案Aが最終的に楽。

---

## 6. 永続化（FS + SQLite）

### 6.1 FS（入力/成果物）

- `data/runs/{run_id}/input/`  : アップロード原本
- `data/runs/{run_id}/work/`   : txt/blueprint/ast/template_draft
- `data/runs/{run_id}/out/`    : template_filled, report, exported
- `data/runs/{run_id}/log/`    : events.jsonl（run_eventsのエクスポート）, agent_debug*.jsonl など
- `data/runs/{run_id}/cache/`  : run専用キャッシュ（任意）

※ `src.tools.analyze_visual_contents` は `data/input` 固定参照だが、現状は
document_process_app側で「run入力（data/runs/{run_id}/input）を読む同名ツール」を注入して回避している。
将来の並列実行を考えると、残る“PoC由来のグローバル/固定参照”も段階的に解消する。

### 6.2 SQLite（履歴/イベント/メタ）

最低限のテーブル:

- `runs(run_id, status, created_at, started_at, finished_at, params_json, error_message, workdir)`
- `run_events(id, run_id, ts, event_type, payload_json)`
- `artifacts(id, run_id, kind, path, created_at, updated_at, meta_json)`

---

## 7. 実装順（MVP）

1. FS上の `run_id` フォルダ作成 + Run一覧/詳細の最低限表示
2. アップロード→Run作成→start（バックグラウンドでダミージョブ）
3. SSEでイベントタイムライン表示（まずはrun_status/stepイベントだけ）
4. 実ジョブを順に接続（PDF変換→blueprint→AST→pre_analysis→execute_analysis）
5. Debugログ/ツールイベントをUIへ（案A or B）
6. 成果物（テンプレ/filled）プレビューとダウンロード
