# 03. ルーティングと情報設計（IA）

## 管理者（既存）

- `/admin` : Run一覧
- `/admin/runs/new` : Run作成
- `/admin/runs/{run_id}` : Run詳細（イベント/パラメータ/FS補完）
- `/admin/documents` : ドキュメント管理

## エンドユーザー（新規）

- `/u` : ユースケース一覧
- `/u/{usecase}` : ユースケース説明 + 開始導線
- `/u/{usecase}/runs/new` : Run作成（最小入力）
- `/u/{usecase}/runs/{run_id}` : Run詳細（成果物中心）
- `/u/{usecase}/runs/{run_id}/partials/*` : HTMX用パーツ
- `/u/{usecase}/runs/{run_id}/artifacts/view|download/...` : out配下中心

## 情報設計の方針

- **入口を分ける**: `/u/{usecase}` は「画面/導線」を分けるための入口。内部実行は `recipe_id` に統一。
- **成果物に最短**: Run詳細は `template_filled.md` を最上段に表示。
- **内部情報は非表示**: `params`, `events`, `work/log/cache` の露出は `/u` では行わない。
