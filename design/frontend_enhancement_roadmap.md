# フロントエンド強化ロードマップ（htmx + Tailwind/DaisyUI）

## 概要

FastAPI + htmx のアーキテクチャを維持しつつ、Tailwind CSS + DaisyUI を導入してデザインとUXを大幅に改善する。

### 方針

- **htmxベースを継続**：既存のSSE/部分更新パターンを活かす
- **Pythonエンジニアだけで完結**：React等のSPAフレームワークは導入しない
- **捨てないプロトタイプ**：既存コードを段階的に拡張
- **LLMフレンドリー**：ChatGPT/ClaudeがHTMLを生成しやすい構成

### 技術スタック（変更後）

| 項目 | 現状 | 変更後 |
|------|------|--------|
| CSS | インラインCSS（base.html内） | Tailwind CSS + DaisyUI |
| アイコン | なし | Heroicons（SVG） |
| クライアントロジック | 素のJS | Alpine.js（任意） |
| Markdownレンダリング | なし | marked.js + highlight.js |

---

## 現在の実装状況

### 実装済み（2026-01-11更新）

- **フェーズ1完了**: Tailwind CSS + DaisyUI 導入
  - `base.html` をリニューアル（ナビバー、ダークモード、フッター）
  - `partials/status_badge.html` 新規作成（ステータス色分け＋アイコン）
  - 全テンプレートをDaisyUIコンポーネントで再実装
- **フェーズ2完了**: UX改善
  - htmx ローディングインジケータ（`hx-indicator`）
  - 確認ダイアログ（`hx-confirm`）
  - トースト通知機能（base.htmlにJS実装）
- **フェーズ3完了**: イベントタイムライン
  - イベント種別ごとの色分け（step/tool/agent）
  - フィルタリング機能（すべて/Step/Tool/Agent）
  - SSEでのリアルタイム更新
- **フェーズ6（部分）**: ダークモード対応
  - ナビバーのトグルで切り替え可能
  - localStorage で設定永続化

### 残課題（将来対応）

- フェーズ4: Markdownレンダリング強化（`artifact_view.html` で一部実装済み）
- フェーズ5: Run作成フォームのドラッグ&ドロップ（Alpine.js導入）
- フェーズ6: レスポンシブ対応のさらなる改善

---

## フェーズ1: デザイン基盤の導入

### 目的

- Tailwind CSS + DaisyUI を導入し、既存UIを置き換える土台を作る

### 実装項目

#### 1.1 base.html の更新

```html
<!doctype html>
<html lang="ja" data-theme="light">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ title or "compare_agent" }}</title>
  <!-- Tailwind CSS + DaisyUI (CDN) -->
  <link href="https://cdn.jsdelivr.net/npm/daisyui@4.12.10/dist/full.min.css" rel="stylesheet">
  <script src="https://cdn.tailwindcss.com"></script>
  <!-- htmx -->
  <script src="https://unpkg.com/htmx.org@1.9.12"></script>
  <script src="https://unpkg.com/htmx.org@1.9.12/dist/ext/sse.js"></script>
</head>
<body class="min-h-screen bg-base-200">
  <!-- ナビゲーション -->
  <div class="navbar bg-base-100 shadow-lg">
    <div class="flex-1">
      <a href="/" class="btn btn-ghost text-xl">compare_agent</a>
    </div>
    <div class="flex-none">
      <a href="/runs/new" class="btn btn-primary btn-sm">新規Run</a>
    </div>
  </div>
  <!-- メインコンテンツ -->
  <main class="container mx-auto p-4 max-w-7xl">
    {% block content %}{% endblock %}
  </main>
</body>
</html>
```

#### 1.2 共通コンポーネント（partials）の作成

- `partials/status_badge.html`: ステータスバッジ（色分け）
- `partials/loading.html`: ローディングインジケータ
- `partials/alert.html`: アラート（成功/エラー/警告）

#### 1.3 ステータスバッジの色分け

```html
<!-- partials/status_badge.html -->
{% if status == "succeeded" %}
  <span class="badge badge-success">{{ status }}</span>
{% elif status == "failed" %}
  <span class="badge badge-error">{{ status }}</span>
{% elif status == "running" %}
  <span class="badge badge-info">{{ status }}</span>
{% elif status == "cancelled" %}
  <span class="badge badge-warning">{{ status }}</span>
{% else %}
  <span class="badge badge-ghost">{{ status }}</span>
{% endif %}
```

### 達成基準

- DaisyUI のコンポーネント（navbar, card, badge, btn）が正常に表示される
- 既存の全ページが新デザインで動作する
- ステータスが色分けされて視認性が向上する

### 推定工数

- 1〜2日

---

## フェーズ2: UX改善（ローディング・フィードバック）

### 目的

- ユーザー操作に対するフィードバックを強化し、処理中の状態を明確にする

### 実装項目

#### 2.1 htmx ローディングインジケータ

```html
<!-- ボタンにローディング表示 -->
<button class="btn btn-primary" 
        hx-post="/runs/{{ run_id }}/start" 
        hx-target="#status-panel"
        hx-indicator="#start-loading">
  <span id="start-loading" class="htmx-indicator loading loading-spinner loading-sm"></span>
  開始
</button>
```

#### 2.2 プログレスバー表示

Run詳細画面にプログレスバーを追加（ステップ進捗を可視化）

```html
<!-- partials/progress.html -->
<progress class="progress progress-primary w-full" 
          value="{{ completed_steps }}" 
          max="{{ total_steps }}"></progress>
<p class="text-sm text-base-content/70">
  {{ current_step }} ({{ completed_steps }}/{{ total_steps }})
</p>
```

#### 2.3 トースト通知

操作完了時にトースト通知を表示

```html
<!-- toast container -->
<div id="toast-container" class="toast toast-end"></div>

<!-- htmx OOB swap でトースト追加 -->
<div id="toast-container" hx-swap-oob="beforeend">
  <div class="alert alert-success">
    <span>Runが正常に開始されました</span>
  </div>
</div>
```

#### 2.4 確認ダイアログ

キャンセル等の破壊的操作に確認ダイアログを追加

```html
<button class="btn btn-error btn-outline"
        hx-post="/runs/{{ run_id }}/cancel"
        hx-confirm="本当にキャンセルしますか？"
        hx-target="#status-panel">
  キャンセル
</button>
```

### 達成基準

- ボタンクリック時にローディングスピナーが表示される
- Run実行中にプログレスバーが進捗を示す
- 操作完了時にトースト通知が表示される
- 破壊的操作時に確認ダイアログが表示される

### 推定工数

- 2〜3日

---

## フェーズ3: イベントタイムラインの視覚化

### 目的

- 実行状況を「フラットなイベント列」ではなく、**Task(Step) → Agent/SubAgent → Tool** の階層として把握できるようにする
- ノードを選択すると右側に **Node Details（メタデータ・直近ログ）** を表示し、原因追跡を速くする

### 実装項目

#### 3.1 階層ツリー + 詳細パネルUIの実装（現行方針）

- 左ペイン: **Workflow Hierarchy（ツリー）**
  - Run → Step(Task) → Agent/SubAgent → Tool の順に表示
  - 実行中は自動的に展開、手動で折りたたみ可能
- 右ペイン: **Node Details**
  - 選択ノードのメタデータ（status/step/parent/tool_call_id等）
  - 直近の関連イベント（SSEのイベントバッファから抽出）

※ 以前検討していた DaisyUI timeline 方式は、階層表現が弱く情報が横に流れて追いにくいため、ツリー表示を優先する。

#### 3.2 イベントの折りたたみ表示

詳細ペイロードは折りたたみ/省略で表示（Node Details側で必要な範囲のみ展開）

```html
<div class="collapse collapse-arrow bg-base-100">
  <input type="checkbox" />
  <div class="collapse-title font-medium">
    {{ event.event_type }} - {{ event.ts | format_time }}
  </div>
  <div class="collapse-content">
    <pre class="text-xs bg-base-300 p-2 rounded overflow-x-auto">{{ event.payload | tojson(indent=2) }}</pre>
  </div>
</div>
```

#### 3.3 イベントフィルタリング

- イベント種別でフィルタリング可能に（ログ一覧側）
- ツリー側は「選択ノードに紐づく直近イベント」を自動フィルタして表示

```html
<div class="flex gap-2 mb-4">
  <button class="btn btn-sm btn-outline" 
          hx-get="/runs/{{ run_id }}/partials/events?filter=step"
          hx-target="#events-container">
    Step
  </button>
  <button class="btn btn-sm btn-outline"
          hx-get="/runs/{{ run_id }}/partials/events?filter=tool"
          hx-target="#events-container">
    Tool
  </button>
  <button class="btn btn-sm btn-outline"
          hx-get="/runs/{{ run_id }}/partials/events?filter=agent"
          hx-target="#events-container">
    Agent
  </button>
</div>
```

### 達成基準

- **階層ツリー**で Task/Agent/Tool が追える
- ノード選択で **Node Details** に直近ログが出る
- ログ一覧は種別フィルタができる
- 破綻しやすい「描画ごとの多重イベントリスナー」などが無い

### 推定工数

- 1〜2日（既存SSEイベントを利用し、UI側の組み立てに寄せる）

---

## フェーズ4: 成果物プレビューの強化

### 目的

- 成果物の閲覧体験を向上させ、Markdown/JSON/テキストを適切にレンダリングする

### 実装項目

#### 4.1 タブUIの実装

成果物をカテゴリ別タブで表示

```html
<div role="tablist" class="tabs tabs-lifted">
  <a role="tab" class="tab tab-active"
     hx-get="/runs/{{ run_id }}/partials/artifacts?category=input"
     hx-target="#artifacts-content">
    入力
  </a>
  <a role="tab" class="tab"
     hx-get="/runs/{{ run_id }}/partials/artifacts?category=work"
     hx-target="#artifacts-content">
    中間生成物
  </a>
  <a role="tab" class="tab"
     hx-get="/runs/{{ run_id }}/partials/artifacts?category=out"
     hx-target="#artifacts-content">
    成果物
  </a>
</div>
<div id="artifacts-content" class="p-4 bg-base-100 rounded-b-box">
  <!-- 成果物一覧 -->
</div>
```

#### 4.2 Markdownレンダリング

`marked.js` + `highlight.js` でMarkdownをレンダリング

```html
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11.9.0/build/highlight.min.js"></script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11.9.0/build/styles/github-dark.min.css">

<div id="markdown-preview" class="prose prose-sm max-w-none"></div>
<script>
  document.getElementById("markdown-preview").innerHTML = 
    marked.parse(`{{ content | escape }}`);
  hljs.highlightAll();
</script>
```

#### 4.3 JSONビューア

JSON成果物（blueprint, AST）をツリー表示

```html
<!-- シンプルなJSON折りたたみ表示 -->
<div class="mockup-code bg-base-300">
  <pre><code class="language-json">{{ content | tojson(indent=2) }}</code></pre>
</div>
```

#### 4.4 差分ビュー（将来）

template_draft と template_filled の差分表示

### 達成基準

- 成果物がカテゴリ別タブで整理される
- Markdownファイルがレンダリングされて表示される
- JSONファイルがシンタックスハイライトされる
- 大きなファイルでもスクロールで閲覧可能

### 推定工数

- 3〜4日

---

## フェーズ5: Run作成フォームの改善

### 目的

- Run作成フォームのUXを改善し、設定項目をわかりやすく整理する

### 実装項目

#### 5.1 ステップウィザード形式

複数ステップに分割してわかりやすく

```html
<ul class="steps w-full mb-8">
  <li class="step step-primary">ファイル選択</li>
  <li class="step">変換設定</li>
  <li class="step">実行オプション</li>
  <li class="step">確認</li>
</ul>
```

#### 5.2 ドラッグ&ドロップアップロード

Alpine.js でドラッグ&ドロップを実装

```html
<script src="https://unpkg.com/alpinejs@3.x.x/dist/cdn.min.js" defer></script>

<div x-data="{ dragover: false }"
     @dragover.prevent="dragover = true"
     @dragleave="dragover = false"
     @drop.prevent="handleDrop($event)"
     :class="{ 'border-primary': dragover }"
     class="border-2 border-dashed rounded-lg p-8 text-center cursor-pointer">
  <p>ファイルをドラッグ&ドロップ、またはクリックして選択</p>
  <input type="file" name="doc_a" class="hidden" />
</div>
```

#### 5.3 フォームバリデーション

クライアントサイドバリデーション + エラー表示

```html
<label class="form-control w-full">
  <div class="label">
    <span class="label-text">doc A (PDF/TXT)</span>
    <span class="label-text-alt text-error">必須</span>
  </div>
  <input type="file" name="doc_a" required 
         class="file-input file-input-bordered w-full" />
  <div class="label">
    <span class="label-text-alt text-error hidden" id="doc_a_error">
      ファイルを選択してください
    </span>
  </div>
</label>
```

#### 5.4 設定のアコーディオン化

詳細設定を折りたたみで整理

```html
<div class="collapse collapse-arrow bg-base-100">
  <input type="checkbox" />
  <div class="collapse-title font-medium">
    詳細設定（PDF変換・LLM）
  </div>
  <div class="collapse-content">
    <!-- PDF変換設定、LLM設定など -->
  </div>
</div>
```

### 達成基準

- フォームがステップ形式で整理される
- ドラッグ&ドロップでファイルをアップロードできる
- バリデーションエラーがわかりやすく表示される
- 詳細設定が折りたたみで整理される

### 推定工数

- 2〜3日

---

## フェーズ6: レスポンシブ対応・アクセシビリティ

### 目的

- モバイル端末でも使いやすいレスポンシブデザインを実現する

### 実装項目

#### 6.1 レスポンシブグリッド

Tailwindのレスポンシブクラスを活用

```html
<div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
  <!-- カード -->
</div>
```

#### 6.2 モバイルナビゲーション

ドロワーメニューの実装

```html
<div class="drawer">
  <input id="my-drawer" type="checkbox" class="drawer-toggle" />
  <div class="drawer-content">
    <!-- ページコンテンツ -->
    <label for="my-drawer" class="btn btn-ghost lg:hidden">
      <svg><!-- ハンバーガーアイコン --></svg>
    </label>
  </div>
  <div class="drawer-side">
    <label for="my-drawer" class="drawer-overlay"></label>
    <ul class="menu p-4 w-80 bg-base-100">
      <!-- メニュー項目 -->
    </ul>
  </div>
</div>
```

#### 6.3 ダークモード対応

テーマ切り替え機能

```html
<label class="swap swap-rotate">
  <input type="checkbox" class="theme-controller" value="dark" />
  <svg class="swap-on"><!-- 太陽アイコン --></svg>
  <svg class="swap-off"><!-- 月アイコン --></svg>
</label>
```

#### 6.4 アクセシビリティ

- 適切なARIA属性
- キーボードナビゲーション対応
- フォーカス表示の改善

### 達成基準

- スマートフォン幅（375px〜）で正常に表示・操作できる
- ダークモードが切り替えられる
- キーボードだけで主要操作が可能

### 推定工数

- 2〜3日

---

## 実装順序のまとめ

| フェーズ | 内容 | 推定工数 | 依存関係 |
|----------|------|----------|----------|
| 1 | デザイン基盤（Tailwind/DaisyUI導入） | 1〜2日 | なし |
| 2 | UX改善（ローディング・フィードバック） | 2〜3日 | フェーズ1 |
| 3 | イベントタイムラインの視覚化 | 2〜3日 | フェーズ1 |
| 4 | 成果物プレビューの強化 | 3〜4日 | フェーズ1 |
| 5 | Run作成フォームの改善 | 2〜3日 | フェーズ1 |
| 6 | レスポンシブ・アクセシビリティ | 2〜3日 | フェーズ1〜5 |

**合計推定工数**: 12〜18日（1人で実施の場合）

---

## ファイル構成（変更後）

```
compare_app/web/
├── app.py                      # FastAPIアプリ（変更なし）
├── templates/
│   ├── base.html               # ★ Tailwind/DaisyUI導入
│   ├── index.html              # Run一覧（リファクタリング）
│   ├── run_new.html            # Run作成（リファクタリング）
│   ├── run_detail.html         # Run詳細（リファクタリング）
│   ├── artifact_view.html      # 成果物閲覧（リファクタリング）
│   └── partials/
│       ├── status.html         # ステータス表示
│       ├── status_badge.html   # ★ 新規: ステータスバッジ
│       ├── artifacts.html      # 成果物一覧
│       ├── template.html       # テンプレプレビュー
│       ├── (removed)           # 旧: timeline.html（階層ツリー + Node Details に置換）
│       ├── progress.html       # ★ 新規: プログレスバー
│       ├── loading.html        # ★ 新規: ローディング
│       ├── alert.html          # ★ 新規: アラート
│       └── toast.html          # ★ 新規: トースト通知
└── static/                     # ★ 新規: 静的ファイル（任意）
    └── js/
        └── app.js              # 共通JS（任意）
```

---

## 検証方法

各フェーズ完了時に以下を確認:

1. **視覚確認**: ブラウザで各ページを表示し、デザインが意図通りか確認
2. **機能確認**: htmxの部分更新、SSEが正常に動作するか確認
3. **レスポンシブ確認**: Chrome DevToolsでモバイル表示を確認
4. **既存機能の回帰確認**: Run作成→実行→成果物閲覧の一連の流れが動作するか確認

---

## 注意事項

- **CDN利用**: 開発・MVP段階ではCDNを使用。本番環境ではローカルにバンドルすることを検討
- **Alpine.js**: フェーズ5で任意導入。htmxで十分な場合は不要
- **段階的移行**: 既存ページを一度に全て書き換えず、1ページずつ移行する
- **テスト**: 各フェーズ完了時にCLI経由でのE2Eテストも実施し、UI変更がバックエンドに影響しないことを確認
