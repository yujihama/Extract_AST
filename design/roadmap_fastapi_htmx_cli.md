## compare_agent アプリ化ロードマップ（FastAPI + HTMX + CLI）

### 現在地（最新）

現時点の実装は以下まで到達しています（詳細は `compare_app/` 配下）。

- **フェーズ0（アプリ土台）**: 完了（SQLite: `runs/run_events/artifacts`）
- **フェーズ1（入口API/ジョブ）**: 完了（`RunExecutor` / `Pipeline` / `InProcessJobQueue` / 協調的キャンセル）
- **フェーズ2（最小UI + SSE）**: 完了（Run一覧/作成/詳細、SSE、テンプレプレビュー、成果物一覧/閲覧/DL）
- **フェーズ3（CLI）**: 完了（`python -m compare_app.cli create/start/execute/cancel/tail/list/artifacts/export`）
- **フェーズ4（PDF/TXT→blueprint→AST）**: 完了（TXT入力実測、PDFはfast/llm変換の両方を実装）
- **フェーズ5/6（Pre-Analysis→Compare-Analysis）**: 完了（`compare_app/core/compare_steps.py`）
  - compare_setup は embedding/LLMキー必須
  - pre_analysis/compare_analysis はキー未設定時フォールバックあり
  - compare_analysis は協調的キャンセルの途中介入に対応

### 次に埋める（優先度高）

- 現時点で優先度高の未完了項目なし
  - PDF→TXT（LLM）: UI/CLIパラメータ対応 + 変換イベント出力まで実装済み
  - 長時間stepの途中キャンセル: `compare_analysis` / `summarize_ast` 対応済み
  - artifacts登録の網羅性: DB優先 + FS補完 + 自動同期でDBを最新化

### 目的

- PoC（`main.py`）の実行フローを **再利用可能なモジュール**に分解し、FastAPI+HTMXのUIで実行・監視・成果物閲覧できるようにする。
- 同じパイプラインを **CLIでも実行**できるようにし、テスト自動化（CIや回帰テスト）に耐える構造にする。
- 後からCeleryに移行しやすい（キュー実装に依存しない）実行基盤にする。

---

## 前提（設計方針）

- **Single source of truth**: UI/CLIは “同一のコア処理” を呼ぶだけにする。
  - `compare_app/core`（ドメイン/パイプライン）
  - `compare_app/infra`（FS/SQLite/EventSink）
  - `compare_app/web`（FastAPI+HTMX）
  - `compare_app/cli`（CLI）
- **run_id中心**: すべての入出力・イベント・状態は `run_id` に紐づく。
- **イベントは案A**: `DebugLoggingMiddleware` 等から `EventSink.emit()` でSQLiteへ保存し、UIはSSEで購読。
- **MVPは同時1 Run**: 排他で開始してOK。ただしrun_id分離は前提。

---

## フェーズ0: リポジトリ骨格（アプリ土台）を作る

### 目的

- “UI/CLIで共有できる”パッケージ構成と、run_id/FS/SQLiteの最小インフラを先に固める。

### 実装項目

- `src/` とは別にアプリ層（例: `app/` か `compare_app/`）を新設
- FSのRunディレクトリ規約（`data/runs/{run_id}/...`）を作り、生成/参照APIを用意
- SQLiteの最小スキーマ作成（`runs`, `run_events`, `artifacts`）
- `EventSink`（SQLite実装）と `RunRepository`（SQLite実装）

### 達成基準（受け入れ条件）

- `run_id` を作成すると `data/runs/{run_id}/` が作られ、SQLiteに `runs` 行が追加される
- `EventSink.emit(run_id, ...)` が `run_events` に保存でき、Run詳細画面で取得可能な形になる
- “アプリ層”が **既存PoCコードに依存せず** import可能（循環参照なし）
  - 現状: `compare_app/` は `src/` に依存しない土台として稼働（realモードの一部で `src/` を利用）

---

## フェーズ1: コア処理の“入口”を統一（UI/CLI共通API）

### 目的

- UI/CLIが呼ぶ “唯一の入口（パイプライン呼び出し）” を確立し、以降はここに処理を繋いでいけるようにする。

### 実装項目

- `Pipeline` もしくは `RunExecutor` を定義
  - `start(run_id, params)`（同期でも非同期でもOK）
  - step単位の `step_started/finished/failed` イベントを必ずemit
- `JobQueue` 抽象 + `InProcessJobQueue` 実装（MVP）
- `cancel_requested` の契約（協調的キャンセル）

### 達成基準（受け入れ条件）

- UI/CLIいずれからでも `RunExecutor.start(run_id)` が呼べる
- 実処理はダミーでも、`queued→running→succeeded` の状態遷移がDBに記録される
- Run詳細で SSE を購読すると、ステップイベントがリアルタイムに表示される

---

## フェーズ2: FastAPI + HTMX（最小UI）を実装

### 目的

- 当初は実処理が未接続でも「Run作成→開始→監視→成果物閲覧」のUI骨格を完成させるのが目的（現在は real も接続済み）。

### 実装項目

- ページ
  - Run一覧: `GET /`
  - Run作成: `GET /runs/new`, `POST /runs`
  - Run詳細: `GET /runs/{run_id}`
- HTMXフラグメント
  - status / artifacts / template preview
- SSE
  - `GET /runs/{run_id}/events`

### 達成基準（受け入れ条件）

- ブラウザから Run を作成し、開始できる（ダミー実行でOK）
- Run詳細画面で SSE によりイベントが増えていくのを確認できる
- UIはページ全体のリロードなしに status/events 部分が更新される

---

## フェーズ3: CLI（テスト自動化の入口）を実装

### 目的

- CI/自動テストでRunを作成・実行・成果物確認できるようにする。

### 実装項目

- CLIコマンド（例）
  - 現状は `python -m compare_app.cli create/start/execute/cancel/tail/list/artifacts/export`
  - 将来は `compare-agent run ...` のようなI/Fに整理しても良い（互換レイヤで吸収）
- CLIは **FastAPIを経由しない**（同一 `RunExecutor` を直接呼ぶ）
  - これにより「UI無しの回帰テスト」が可能になる

### 達成基準（受け入れ条件）

- CLIで Run を作成→開始→完了まで待機でき、終了コードが `0`（成功）/`!=0`（失敗）で分かれる
- CLI実行でも `runs/run_events/artifacts` が一貫して記録される（UIと同じ）
- `compare-agent run tail` で tool/agentイベントが追える（後続フェーズで詳細化）

---

## フェーズ4: パイプライン実接続（PDF/TXT→blueprint→AST）

### 目的

- “比較前”の生成系（PDF変換、blueprint、AST）をRunパイプラインに接続する。

### 実装項目

- 入力取り込み（FS保存）→必要に応じて PDF→TXT 変換
- blueprint生成 deep_agent 実行
- blueprint検証/プレビュー（自動 or UIで手動）
- AST生成
- 生成物をartifactとして登録し、UI/CLIから閲覧/ダウンロード可能にする

### 達成基準（受け入れ条件）

- 2本のPDF（またはTXT）から、Run内に `*.txt`, `*_blueprint.json`, `*.ast.json` が生成される
- UIの成果物一覧に、上記artifactが **種類付き**で表示され、プレビューできる
- 失敗時は `failed` になり、どのstepで落ちたかがイベント/ログで追える

---

## フェーズ5: Pre-Analysis（関係性判定＋テンプレ生成）を接続

### 目的

- 静的テンプレに依存せず、**Pre-Analysisでテンプレを生成**する仕様を実現する。

### 実装項目

- Pre-Analysis deep_agent の実行（docA/docB ASTを投入）
- 生成テンプレ（`template_draft.md` 相当）を artifact として保存

### 達成基準（受け入れ条件）

- Run内に「生成テンプレ」が保存され、UI/CLIで閲覧できる
- Pre-Analysisの出力（relation/reason/plan）がRunメタに保存される（DBのparams_json等でも可）

---

## フェーズ6: 比較分析（テンプレ段階更新）を接続

### 目的

- “最終成果物=filled template” を確実に生成し、途中経過も追える状態にする。

### 実装項目

- compare_setup / embedding / matching をRunに接続（キャッシュは既存JSONを使用）
- compare_analysis deep_agent の実行（テンプレを段階編集）
- `template_filled.md` を更新し続ける（必要なら版管理）

### 達成基準（受け入れ条件）

- Run内に `template_filled.md` が生成され、UIで内容が確認できる
- 実行中、UIのテンプレプレビューが（イベントを契機に）更新され、段階的に埋まっていく
- 実行ログ/イベントで「どのsubagent/toolがテンプレのどの変更に寄与したか」を追える（最低でも時系列で追跡可能）

---

## フェーズ7: 運用性/拡張（将来）

### 7.1 waiting_user（人手介入）フロー

- blueprint編集・テンプレ編集・パラメータ調整→再開

**達成基準**
- Runを `waiting_user` に遷移でき、UIから再開できる

### 7.2 同時実行（run_id分離の完成）

- InProcessで複数Run→将来Celeryへ

**達成基準**
- 2つのRunを並行に走らせてもイベント/成果物が混ざらない

### 7.3 Celery移行

- `JobQueue` 実装の差し替えのみで移行可能にする

**達成基準**
- `InProcessJobQueue` と `CeleryJobQueue` を入れ替えてもUI/CLIのAPIが変わらない

---

## ロードマップの“都合の良い解釈”を防ぐルール

- 各フェーズの達成基準は **必ず手順付きで検証可能**にする（UI操作 or CLIコマンドの結果で判定）
- 「できた」の定義は “画面がある” ではなく、**run_id/イベント/成果物が一貫して残る**こと
- UIとCLIの差異を許容しない（同じRunExecutor/同じDB/同じartifact規約で動くこと）
