# FastAPI + HTMX アプリ化 要件整理（document_process_agent）

## 目的と前提

- `main.py` のセル実行フロー（PDF→txt→blueprint→AST→比較種別判定→比較→成果物保存）を **モジュール化**し、FastAPI + HTMX で **ジョブ実行型アプリ**として提供する。
- **CLIでも同じ処理を実行できる設計**にする（テスト自動化のため）。
  - FastAPI/HTMXはUI層として位置づけ、コア処理は「UI/CLI共通のライブラリAPI」として実装する。
- **テンプレートは `templates/` 配下の静的ファイルを使わない**。
  - 「Pre-Analysis（タスク計画）」の段階で、**複数ドキュメント（1件以上）＋依頼文**から **結果記入用テンプレート（Markdown等）を生成**する。
  - 最終成果物は「生成テンプレートに段階的に記入した結果（filled template）」。
- deep_agent は自律的に sub agent / tool を呼び出す。
  - **その過程（どの subagent が何をし、どの tool が何回/何を引数に実行されたか）を UI 上で追える**ことが望ましい。
- 認証は不要（単一ユーザー想定で開始）。
- エージェントがファイル参照ツールで参照するため、**入力/成果物はファイルシステム**で保持するのが自然。
  - 一方、**実行履歴・メタ情報・進捗・イベント・埋め込みキャッシュ管理**は SQLite を検討。
- 実行基盤は将来的に Celery を使いたいが、最初は不要。
  - **後から Celery に移行しやすいインターフェース設計**が必須。

---

## 1. コア機能要件（MVP）

### 1.1 Run（実行）管理

- **Run 作成**: **1件以上の入力ドキュメント（documents）** と実行パラメータをまとめた実行単位 `run_id` を発行
- **状態**: `queued / running / succeeded / failed / cancelled` を保持
- **進捗**:
  - ステップイベント（`step_started/step_finished/step_failed/step_skipped`）を EventSink に保存
  - パーセンテージやメッセージの集計は未実装（UIはイベントタイムラインで追跡）
- **成果物**:
  - 入力ファイル、生成txt、blueprint、ast、embedding cache、生成テンプレ、filled template、ログ（JSONL/レポート）を Run に紐付けて一覧/閲覧/ダウンロードできる
- **ステップ選択パラメータ（step filtering）**:
  - `steps_include`: 実行するステップ名のリスト（例: `["build_blueprint_all", "pre_analysis"]`）
  - `step_from` / `step_to`: 範囲指定（開始〜終了ステップ名）
  - **UI対応済み**: Run作成画面で `step_from` / `step_to` を選択可能
  - 詳細は `design/run_executor_pipeline_api.md` を参照

### 1.2 入力取り込み

- **アップロード**: PDF/TXT をアップロードして Run に紐付けて保存
- **テキスト入力**: 直接貼り付け（小さいデータ用、任意）
  - 現状は UI 未対応。JSON API（`POST /api/runs/multi`）で `documents[].text` として作成可能
- **前処理**:
  - PDF→txt 変換（高速モード / LLMモード）
  - txt はそのまま利用（エンコーディングは `errors=replace` 等で扱う）

### 1.3 PDF→TXT 変換

- **高速モード**: 既存 `convert_pdf_to_txt`（表/視覚要素マーカー挿入）
- **LLMモード**: 既存 `convert_pdf_with_llm`（ページ並び替え、`<!-- VISUAL_CONTENT -->`/`<!-- AGENDA -->` 等のタグ付与）
- **パラメータ**:
  - `start_page`, `end_page`, `batch_size`, `use_image`
- **UI要件**:
  - `documents` 入力（複数ドキュメント）の場合は **全ドキュメント共通**で `pdf_mode=fast|llm` を選べる
- **ジョブ化**: 変換は長時間になりうるためバックグラウンド実行が必須

### 1.4 Blueprint 生成/検証/編集

- **生成（LLM）**: txt を入力として `DocumentStructureBlueprint` を生成
- **検証**: gaps/irregular/titles、見出しツリーのプレビュー
- **編集**:
  - MVP では「JSONの直接編集」でも可（テキストエリア）
  - できれば rule 単位編集（regex + validation_rules）に将来拡張

### 1.5 AST 生成/閲覧

- **生成（非LLM）**: blueprint + txt から AST を生成
- **枝サマリ（LLM, 任意）**: 非leafノードに `content_summary` を付与（コスト増、UIでON/OFF）
- **閲覧**:
  - outline（深さ指定）、タイトル検索、node_path 指定表示（既存 `read_ast` に相当するUI）

### 1.6 比較種別判定（Pre-Analysis）＋テンプレ生成（重要）

- **入力**: 依頼文（`request_text`）＋ドキュメント一覧（`doc_id`/`doc_hash` 等）＋各ドキュメントの txt/blueprint/AST（必要に応じて）
- **出力**:
  - `documents`（doc_id/filename/role_guess 等）
  - `execution_plan`（機械可読な実行計画。必要なら比較ペア準備 `pair_setup` を含む）
  - `template`（テンプレファイル名。本文は `work/template_draft.md` として保存）
  - `is_complete`（true の場合、pre_analysisで完結し `out/template_filled.md` を生成）
  - `filled_report`（`is_complete=true` の場合の最終レポート本文）
- **要件**:
  - 生成テンプレは Run 成果物として保存され、以降の比較分析がそのテンプレを段階的に編集して埋める

### 1.7 比較分析（テンプレ段階更新）

- **マッチング**:
  - all-chunk similarity matching（ハイブリッド: cosine + keyword + title keyword）
  - 統計/詳細（未マッチの列挙）
- **差分抽出**:
  - 高類似度: diff（LLM不要）
  - 低類似度: LLM差分抽出（構造化JSON）
- **テンプレ更新**:
  - テンプレを一括で最後に埋めるのではなく、**途中経過も成果物として更新**される（UIで追える）

### 1.8 実行過程の可視化（UI要件の核）

deep_agent が自律的に sub agent / tool を呼ぶため、UIには以下が必要:

- **イベントタイムライン**:
  - agent_start / agent_end
  - before_model / after_model（任意）
  - tool_call_start / tool_call_result / tool_call_error
  - subagent の親子関係（可能な範囲で）
- **内容表示レベル**:
  - デフォルトは「要約」（ツール名、引数キー、所要時間、ステータス）
  - クリックで詳細（引数全文、result preview、エラー全文）
- **リアルタイム更新**:
  - HTMX + SSE（推奨）で Run 詳細画面の一部をストリーム更新

---

## 2. 非機能要件（MVP）

- **単一ユーザー/認証なし**
- **同時実行**: MVPは「同時1 Run」でも可（ただし将来の並列実行に備え、run_id 分離が前提）
- **耐障害性**:
  - 失敗時に `failed` とエラー原因（例外/ログ）へ誘導
  - 再実行（同じ入力で再Run、または同Run再開）は将来要件
- **コスト/安全**:
  - LLM呼び出し回数/ページ範囲/バッチサイズ等をUIで制御
- **Windows対応**（現状の開発環境前提）

---

## 3. データ/保存設計（推奨）

### 3.1 ファイルシステム（Artifacts）

- ルート: `data/runs/{run_id}/`
  - `input/`（アップロード原本）
  - `work/`（中間生成物: txt/blueprint/ast/template等）
  - `out/`（最終成果物: filled template, report, exported）
  - `log/`（JSONLなど）
  - `cache/`（embedding cache 等：run専用 or 共有）

※ 既存実装が `data/input` を前提としている箇所があるため、最初は
「`data/input` にコピーして処理」でも良いが、将来の並列実行を考えると
**入力ディレクトリを run_id で分離できるように改修**するのが望ましい。

### 3.2 SQLite（メタ・履歴・イベント）

MVPで入れるなら最小で以下:

- `runs`
  - `run_id`（PK）
  - `status`, `created_at`, `started_at`, `finished_at`
  - `params_json`（変換/比較/LLM設定）
  - `documents[].doc_hash`（入力ドキュメントの参照用）
  - `error_message`（失敗時）
- `run_events`
  - `id`（PK）
  - `run_id`（FK）
  - `ts`
  - `event_type`（tool_call_start等）
  - `payload_json`（要約＋必要に応じて全文）
- `artifacts`
  - `id`（PK）
  - `run_id`（FK）
  - `kind`（固定enumではなく文字列。`artifact_updated` のpayloadをそのまま保存）
  - `path`
  - `created_at`

Embedding cache は巨大化しがちなので、当面は既存の JSON ファイルキャッシュを使い、
SQLiteは「どのcacheを使ったか/統計」だけ持つのが現実的。

---

## 4. 実行基盤（Celery移行を見据えた要件）

### 4.1 抽象インターフェース（必須）

アプリ側は「キュー実装」に依存しないようにする。

- `JobQueue.enqueue(run_id, job_type, payload) -> job_id`
- `JobQueue.cancel(run_id) -> bool`
- `JobQueue.get_status(run_id) -> status`
- `EventSink.emit(run_id, event)`

### 4.2 MVP実装（非Celery）

- In-process worker（スレッド or asyncio task）
- `run_id` 単位で排他（MVPは同時1でもOK）
- 進捗は EventSink に流す（SQLite or メモリ）＋ UI は SSEで購読

### 4.3 将来（Celery）

- `JobQueue` 実装を Celery へ差し替え
- EventSink は
  - 1) ワーカーがDBへ直接書く、または
  - 2) Redis pubsub を併用（将来）

---

## 5. UI（FastAPI + HTMX）要件

### 5.1 画面

- `/` Run一覧（状態、入力、作成日時、直近更新、リンク）
- `/runs/new` Run作成（アップロード→変換設定→開始）
- `/runs/{run_id}` Run詳細
  - ステータス、進捗、成果物一覧
  - 「イベントタイムライン」（SSEで自動更新）
  - 템プレ生成物/filled template のプレビュー（部分更新）

### 5.2 SSE（HTMXでリアルタイム）

- `/runs/{run_id}/events`（text/event-stream）
- フロントは「新規イベントを追記表示」し、必要なら成果物プレビュー領域もhx-getで更新

---

## 6. 実装上の注意（現行コード起因の要件）

- `COMPARE_STATE`（グローバル状態）は Webアプリでは危険
  - MVPは「同時1Run」制限で逃げられるが、将来のために `run_id` で状態分離できる構造にする
- ログ（JSONL）は run_id ごとに出力先を変える必要がある（衝突防止）
- `analyze_visual_contents` 等が `data/input` 固定参照のため、run分離する場合は改修が必要
- 外部API（OpenAI/Azure）の **クォータ/請求エラー（例: 429 insufficient_quota）** は発生し得る
  - Runは `failed` とし、UI/CLIで原因が分かるようにエラーメッセージを保存・表示する

---

## 実装状況（最新）

この要件に対して、現在 `document_process_app/` を追加して **UI/CLI共通の実行土台**まで実装済みです。

### 実装済み（MVP土台）

- **UI/CLI共通の入口API**:
  - `RunExecutor` / `Pipeline` を実装（`document_process_app/core/`）
  - `mode=dummy|real` による経路分岐（`step_skipped` をemit）
- **永続化（SQLite）**:
  - `data/document_process_app.db` に `runs` / `run_events` / `artifacts` を保存（`schema_version`あり）
  - `artifacts` は `artifact_created` / `artifact_updated` イベントで自動upsert
- **ファイル配置（FS）**:
  - `data/runs/{run_id}/input|work|out|log|cache` を作成
  - 入力アップロード/CLI指定パスを `input/doc_<doc_id>.*` として保持
  - Run完了時に `log/events.jsonl`（run_eventsのエクスポート）を生成
- **バックグラウンド実行（非Celery）**:
  - `InProcessJobQueue`（別スレッド）で `RunExecutor.execute()` を実行
- **可視化（案A）**:
  - Pipelineの `step_*` イベントを `EventSink`（SQLite）へ保存
  - deep_agent の tool/subagent 呼び出しを `EventSinkMiddleware` で `EventSink.emit()`（`document_process_app/agents/middleware.py`）
- **COMPARE_STATE（比較用グローバル状態）の混線対策**:
  - `src.tools.COMPARE_STATE` を **スレッドローカルProxy** に差し替え（`document_process_app/compat/patch_src_tools.py`）
  - InProcessJobQueue（スレッド実行）で複数Runを動かしても混線しにくい形にした
  - `compare_*` 系ツールは **doc_a_id/doc_b_id を必須**にし、ペアごとの状態復元を行う（pair_dir 永続化を利用）
  - 本文検索は `search_by_keyphrase(file_path, phrase, intent)` で **単一AST** を対象に実行する（ペア非依存）
- **Web UI（FastAPI + HTMX）**:
  - Run一覧/作成/詳細、SSEでイベント追跡、テンプレ（draft/filled）プレビュー
  - 成果物一覧（DB優先＋FS補完）/プレビュー（テキスト）/ダウンロード
  - agent/tool/subagent系イベントもタイムラインに表示（dummyで疑似発火して動作確認可能）
- **CLI**:
  - `python -m document_process_app.cli create/start/execute/cancel/tail/list/artifacts/export` を実装（FastAPIを経由せず `RunExecutor` を直接使用）

- **UI未実装の要件はAPIで提供（完了扱い）**:
  - テキスト貼り付け入力 / blueprint検証 / AST閲覧（search等）/ イベント一覧（SSE代替）/ 比較統計の参照
  - 提供APIの一覧は `design/fastapi_htmx_implementation_features.md` の「2.6 JSON API」を参照

### 一部実装（realモードの骨格）

- PDF/TXT → txt（fast/llm） → blueprint（LLM） → AST（非LLM） のステップを追加
  - `document_process_app/core/real_steps.py`
  - ※ blueprint生成は LLMキーが必要（未設定時はエラーになる）
- Pre-Analysis（タスク計画＋テンプレ生成） → Compare-Analysis（テンプレ埋め） のステップを追加
  - `document_process_app/core/compare_steps.py`
  - ※ `pre_analysis` / `execute_analysis` は LLMキーが必要（未設定時は失敗）
- 必要な比較準備は `pair_compare_setup` ツールで必要ペアだけ準備する

### 検証済み（realモードの本番接続）

- **TXT入力**で、以下の end-to-end が **`succeeded` まで完走**することを確認済み:
  - txt → blueprint（LLM）→ AST（非LLM）→ pre_analysis（計画＋テンプレ生成）→ execute_analysis（計画に従い必要ペアを準備しつつテンプレ記入）→ `template_filled.md`

### 未実装（この要件の“本丸”）

- **PDF→TXT（LLMモード）の実測検証**（コスト/時間/品質）
  - 実装: UIで選択・パイプライン接続済み（`pdf_mode=llm`）
  - 未: 実PDFでの安定運用（ページ範囲・batch_size・use_image）を含む検証
- **artifactsテーブルを“完全”に活用**（SQLite＋UIの成果物一覧を拡張）
  - 現状: stepごとの `artifact_updated/created` に加え、Run終了時にFSをスキャンして `artifacts` を補完する（kindは基本 `file`、既存kindは上書きしない）
  - 未: kind体系の整理（汎用 `file` から用途別kindへの昇格ルール、meta拡張）
- **長時間ステップの協調的キャンセル（step内）**
  - 現状: Pipelineは step境界で `CancellationToken` をチェック
  - 現状: deep_agentの tool 呼び出し境界・ASTサマリのループ境界でキャンセル可能（LLM呼び出し中の即時中断は不可）
- **run分離の完全化**（`data/input` 固定参照や `COMPARE_STATE` 依存の段階的解消）
  - 現状: `COMPARE_STATE` はスレッドローカル化したが、永続化/プロセス分離（Celery）前提の整理は未完
