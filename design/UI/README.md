# UI設計ドキュメント（design/UI）

## 目的

このフォルダは、現状の `document_process_app` のUIが **「管理者（開発/運用）向けに情報が見えすぎる」** 状態であることを踏まえ、

- 管理者UIは維持（デバッグ/運用に必要）
- エンドユーザーUIは **ユースケース別URL** で提供
- エンドユーザーUIは **現代的で操作性の高いUX**（迷わない導線、情報の段階的開示、成果物中心）

を実現するための設計方針・情報設計・ルーティング案・安全/権限の考え方・移行計画を記録します。

本リポジトリは FastAPI + Jinja + HTMX を採用しており、SPA全面移行をしなくても十分にモダンな体験を作れます。

## 重要な前提（この設計の“核”）

- **Run/Executor/保存（DB・FS）のコアは共有**し、UIは「導線/入力ガイド/見せ方」だけを分離する。
- ユースケース別URLは **画面/テンプレを分ける**ための入口であり、内部実行は **レシピ（recipe_id）** の選択で統一する。
- “見せない”だけでは事故るため、削除や内部ファイル閲覧などは **サーバ側でも遮断**する。

## 既存実装との対応関係（要点）

- 現行UI（管理者寄り）
  - Run一覧: `document_process_app/web/templates/index.html`
  - Run作成: `document_process_app/web/templates/run_new.html`
  - Run詳細（イベント詳細・成果物フル等）: `document_process_app/web/templates/run_detail.html`
  - ドキュメント管理: `document_process_app/web/templates/documents.html`
  - ルーティング実装: `document_process_app/web/app.py`

## ドキュメント構成（読む順）

- `01_current_state_and_gap.md`
  - いま何が「管理者向け」になっているか、UI/エンドポイント単位で整理
- `02_enduser_ux_principles.md`
  - エンドユーザーUIの体験原則（ウィザード化、段階的開示、成果物中心、失敗時のガイド）
- `03_routes_and_ia.md`
  - 管理者UIとエンドユーザーUIのURL分離、画面遷移、情報設計（IA）
- `04_usecase_recipes.md`
  - ユースケース別URLとレシピ定義（入力ロール、テンプレ方針、推奨設定）
- `05_security_and_safety.md`
  - RBAC/ガード、削除・イベント詳細・成果物範囲（work/log/cache）などの安全設計
- `06_migration_plan.md`
  - 段階導入、既存UI温存、計測、テスト/運用手順

## 意図と経緯（なぜ今この設計が必要か）

`document_process_app` は PoC（`main.py`）を「Run管理・履歴・成果物閲覧・イベント可視化」できるジョブ実行型アプリに拡張するために導入されました（背景は `design/story_compare_app.md`）。

その結果、デバッグに必要な情報（イベントpayload、FS成果物の広範囲スキャン、params JSONなど）がUIに集約され、MVPとしては合理的ですが、エンドユーザー体験としては

- 情報過多で迷う
- 誤操作（削除/内部閲覧）のリスク
- 「成果物に最短で到達できない」

という課題が顕在化します。

このフォルダは、そのギャップを埋めるための **UI/UXの管理方法**（共通コア＋入口分離＋レシピ化＋安全ガード）を設計として固定化するために作成しました。

