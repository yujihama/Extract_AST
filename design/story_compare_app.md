# document_process_app 実装の経緯（ストーリー / 仕様決定の背景）

## 0. 目的（なぜ作ったか）

このリポジトリの `main.py` は、PoCとして「PDF/TXT→blueprint→AST→比較→テンプレ記入→成果物保存」を1本で回せる一方、

- 実行単位（run）や履歴管理が無い
- UIでの実行/監視/成果物閲覧ができない
- グローバル状態（例: `src.tools.COMPARE_STATE`）があり並列実行に弱い
- 途中経過（subagent/tool呼び出し）が見えづらい

という “アプリ化に必要な土台” が不足していました。

そのため、**ジョブ実行型のWebアプリ**（FastAPI + HTMX）へ移行できるように、実行を `run_id` で管理する `document_process_app/` を追加しました。

---

## 1. 仕様の前提（早めに固めたこと）

- **テンプレは静的ファイルを使わない**
  - Pre-Analysisで「依頼文＋複数ドキュメント（1件以上）の役割推定／実行計画（execution_plan）」と同時に「記入用テンプレ（draft）」を生成し、最終成果物は `template_filled.md` とする。
- **UI/CLI共通の入口API**
  - UIだけでなくCLIでも同じ処理を呼べるようにし、回帰テスト/自動化を可能にする。
- **イベント可視化（deep_agentの過程）**
  - agent_start/end、tool_call_* を保存し、UIで追跡できるようにする（SSEでリアルタイムに表示）。
- **永続化の分離**
  - 大きいファイルはFS、履歴/メタ/イベントはSQLite。
- **ジョブ基盤の差し替え**
  - MVPは in-process で走らせ、将来 Celery に差し替えられるよう抽象を設ける。

---

## 2. 実装のストーリー（段階）

### フェーズA: “走る土台” を最優先で作る

- `RunExecutor` / `Pipeline` を作り、UI/CLIから同じ入口で実行できるようにした。
- `JobQueue` を抽象化し、MVPでは `InProcessJobQueue`（別スレッド）で開始。
- `EventSink` / `RunRepository` をSQLite実装にし、run状態とイベントを永続化。

狙い:
- PoCロジックを繋ぐ前に「Runが作れて、走って、履歴が残って、UIで追える」ことを先に保証する。

### フェーズB: 可視化を “ログファイル” ではなく “イベント” に寄せる

- `EventSinkMiddleware` を実装し、deep_agent/subagent/tool の実行イベントを `run_events` に保存。
- UIは `/runs/{run_id}/events`（SSE）で購読し、タイムライン表示する。

狙い:
- 「後からJSONLをtail→DBへ取り込み」より、リアルタイム性と拡張性が高い。

### フェーズC: 成果物の管理と閲覧導線を作る

- FS: `data/runs/{run_id}/input|work|out|log|cache`
- SQLite: `artifacts` テーブルを追加し、`artifact_created/updated` イベントで upsert
- UI: 成果物一覧（DB優先＋FS補完）→ view/download
- Run完了時に `log/events.jsonl` をエクスポート（run_eventsのコピー）

狙い:
- 「どこに何が生成されたか」をUI/CLI双方から確認でき、デバッグ/監査を容易にする。

### フェーズD: PoCの主要ステップを “Step” として接続する（realモード）

realモードのステップ（概略）:

- EnsureText（TXT or PDF→TXT）
- BuildBlueprint（LLM）
- BuildAst（非LLM）
- （任意）SummarizeAst（LLM）
- CompareSetup（Embedding/Index）
- PreAnalysis（関係性分析＋テンプレ生成）
- ExecuteAnalysis（テンプレ記入＝最終成果物）

狙い:
- `main.py` の主要フローを “Step” に分解し、UI/CLIで同一実行できる形に置き換える。

---

## 3. 現状の到達点（何ができるか）

- TXT入力で end-to-end（txt→blueprint→AST→pre/execute analysis→filled）を完走（実測）
- PDF入力では fast変換を real パイプラインに組み込み（UIで選択可能、実測）
- deep_agent の tool/subagent イベントをUIで追跡可能（SSE）
- 成果物はUI/CLI/FSで閲覧可能

---

## 4. 残タスク（拡張のための論点）

- **PDF→TXTの LLMモードの実測検証**（コスト/時間/ページ設定）
- **長時間ステップのキャンセル**（execute_analysis/summarize_ast など step内で協調的キャンセル）
- **COMPARE_STATEの完全分離**（Celery/プロセス分離を見据え、run_id単位の状態管理へ）
- **artifactsの網羅性**（生成物の登録漏れを潰す、meta拡張）
- **UIの細部**（フィルタ/検索、イベント詳細表示、進捗メータ、large payload対策）

