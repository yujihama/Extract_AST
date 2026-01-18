# ドキュメント中心アーキテクチャ設計（実装準拠）

## 1. 概要

入力ファイル（ドキュメント）を独立したエンティティとして永続化し、AST/Blueprint などを **Runを跨いで再利用**できるようにする。
Run は「実行単位（状態/イベント/最終成果物）」を持ち、入力ドキュメント自体の同一性は `doc_hash` によって管理する。

---

## 2. ディレクトリ構造（現行）

```
data/
  ├── documents/                      # ドキュメント単位で管理
  │   └── {doc_hash}/                 # SHA256先頭16文字
  │       ├── raw.txt                 # 元テキスト（正規化済み/utf-8想定）
  │       ├── ast.json                # AST（必要に応じて content_summary 付きで更新される）
  │       ├── blueprint.json          # Blueprint（LLM生成）
  │       └── metadata.json           # メタデータ（has_ast/has_blueprint 等）
  │
  ├── document_pairs/                 # ドキュメントペア（キャッシュ/再利用向け）
  │   └── {pair_hash}/                # sha256("{doc_a_hash}_{doc_b_hash}")[:16]
  │       ├── metadata.json
  │       ├── initial_matching.json
  │       └── embedding_cache.json
  │
  └── runs/
      └── {run_id}/
          ├── config.json             # 入力/設定（documents, request_text, template_seed_path 等）
          ├── input/                  # doc_<doc_id>.* を配置
          │   └── template_seed.(md|txt)  # 任意: アップロードされたテンプレ seed（Runに紐付く）
          ├── work/                   # 中間生成物（doc_<id>.txt, blueprint_<id>.json, ast_<id>.ast.json 等）
          │   ├── docs/
          │   │   └── index.json      # pre_analysis/execute用のドキュメント索引
          │   └── pre_analysis.json   # pre_analysis の出力（execution_plan 等）
          ├── out/
          │   └── template_filled.md  # 最終成果物
          ├── log/
          │   └── events.jsonl        # run_events のエクスポート
          └── cache/                  # 一時キャッシュ（embedding_cache 等）
```

補足:
- **N-doc のペア比較準備**は、`work/pairs/{a}_{b}/...` のように **runローカル**へ作る（Windowsのファイルロック競合を避ける意図）。
- `document_pairs/` は「同一ペアを別runで再利用」するための共有キャッシュとして残している。

---

## 3. 入力（documents）

### 3.1 documents（正）

- Runは `documents`（1件以上）を受け取り、各ドキュメントを `doc_id` で識別する。
- `doc_id` はファイル名に使える安全な文字へ正規化され、`input/doc_<doc_id>.*` として配置される。
- `config.json` には `documents: [{doc_id, doc_hash, filename, source...}, ...]` が保存される。

## 4. 主要コンポーネント（現行実装の対応）

- `compare_app/infra/document_store.py`
  - `DocumentRepository`: `data/documents/` の永続化
  - `DocumentPairRepository`: `data/document_pairs/` の永続化
- `compare_app/infra/fs_artifacts.py`
  - `FileArtifactStore`: Runディレクトリ作成、入力の配置、doc_repo/pair_repo との連携
- `compare_app/core/run_executor.py`
- `RunExecutor.create_run`: `documents` でRun作成
  - run作成時に `config.json` を更新し、`documents_linked` 等のイベントを記録

---

## 5. 運用メモ（削除/リセット）

既存Runを削除して作り直す（PowerShell例）:

```powershell
Remove-Item -Recurse -Force .\data\runs\*
```
