# 01. 現状整理：管理者UIとしての成立と、エンドユーザーUXとのギャップ

## 対象範囲

- UI: `document_process_app/web/templates/*.html`
- ルーティング/挙動: `document_process_app/web/app.py`

この章では「現状UIが“管理者向け”としては合理的である」一方、「エンドユーザー向けとしては露出情報が多すぎる/導線が重い」点を、根拠付きで整理します。

## 現状UI（主に `/admin`・`/admin/runs/new`・`/admin/runs/{run_id}`）の特徴

### 1) 「内部状態の可視化」が常時ONになっている

#### 1-1) RunパラメータがJSON全文で表示される

- Run詳細の「パラメータ」カードで `run.params` を `tojson` して表示している。
- これはデバッグ/再現性/運用には便利だが、エンドユーザーには意味不明になりやすい。

該当: `document_process_app/web/templates/run_detail.html`

#### 1-2) イベントログが payload（tool引数など）全文を含めて表示される

- `initial_events` を描画し、`ev.payload | tojson(indent=2)` を常に表示している。
- SSEでリアルタイム追記も行っており、デバッグとしては強力。
- しかし、機密情報（入力断片、ツール引数、失敗時のスタック/例外断片）が混入する可能性がある。

該当: `document_process_app/web/templates/run_detail.html`

#### 1-3) 「成果物一覧」が FS を広範囲にスキャンして補完する

サーバ側では artifacts DB を優先しつつ、未記録ファイルがある場合に `input/work/out/log/cache` を `rglob` で走査して補完している。

- 管理者には便利（生成物漏れを拾える）
- エンドユーザーにはノイズ（work/log/cache が混ざる）
- さらに「内部ファイル名/ディレクトリ構成」自体が漏れる

該当: `document_process_app/web/routes/admin.py` の `/admin/runs/{run_id}/partials/artifacts`

### 2) 破壊的な操作が一般UIに露出している

#### 2-1) Run削除ボタンがRun一覧に表示される

- `hx-delete="/admin/runs/{run_id}"` が一覧画面にある。
- 管理者には必要だが、エンドユーザーに公開すると事故りやすい。

該当: `document_process_app/web/templates/index.html`

#### 2-2) ドキュメント削除が一般UIに露出している

- `hx-delete="/admin/documents/{doc_hash}"` がドキュメント一覧で可能。
- 「入力資産の破壊」に当たるため、ロール/権限で制限すべき。

該当: `document_process_app/web/templates/documents.html`

### 3) Run作成画面が「万能フォーム」になっている

`/admin/runs/new` は、MVPとして必要なスイッチが一通りある（PDF変換、AST枝サマリ、モデル、ステップ範囲、HITL、テンプレアップロード等）。

しかしエンドユーザー視点では

- どれが必須で、どれが高度設定か判別しづらい
- ユースケース（準拠評価/差分/抽出…）に応じた入力ガイドが弱い
- 「成果物に最短で到達する導線」より「デバッグの自由度」が優先されている

該当: `document_process_app/web/templates/run_new.html`

## ギャップの整理（管理者UIとしての強み / エンドユーザーUXとしての弱み）

### 管理者UIとしての強み（残すべき理由）

- 実行パイプラインの状態が細かく追える（イベント/ツリー）
- 失敗時の原因究明がしやすい（payload/例外断片）
- FS成果物を広く見られる（生成物漏れの確認、デバッグ）
- 任意の設定を触れて再現・試行錯誤ができる（Run作成画面）

### エンドユーザーUXとしての弱み（分離が必要な理由）

- 情報過多で「今やるべきこと」が埋もれる
- 破壊操作が近い（削除等）
- 内部情報の露出（params、work/log/cache、tool引数）＝漏えいリスク
- “ユースケースの言葉”で導線が組まれていない（準拠評価/差分/抽出など）

## ここからの設計方針（次章への繋ぎ）

1. **管理者UIは温存**し、ルート/URLを `/admin/...` に寄せる。
2. エンドユーザーUIは `/u/{usecase}` のように **ユースケース別URL** を入口として提供する。
3. 実行の中身は分岐させず、Runには `recipe_id` を保存し、テンプレ生成/入力ロール/推奨設定を「レシピ」として管理する。
4. エンドユーザーUIは「成果物中心」「ウィザード」「段階的開示」「安全ガード」を前提に再設計する。

