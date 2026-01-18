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

### 2.1 compare_state の管理（実装詳細）

**現状**:
- `src/tools.py` の `COMPARE_STATE` は **スレッドローカル辞書** として実装されている
- エージェント実行時、別スレッドで tool が実行される場合、`COMPARE_STATE` が空になる問題がある
- `compare_app/agents/middleware.py` の `EventSinkMiddleware` が tool 呼び出し前に自動復元を行う

**復元の仕組み**（`middleware.py:324-455`）:
1. `compare_*` ツール呼び出し時、`COMPARE_STATE` が空なら自動復元を試みる
2. `CompareRestoreConfig` に基づき、`compare_setup` を再実行して状態を復元
3. run単位のロックで同時復元を抑制
4. `initial_matching` があれば `work/pairs/{a}_{b}/initial_matching.json` から読み込む

**注意事項**:
- `compare_setup` は状態を `COMPARE_STATE.clear()` でリセットするため、復元後は必ず新しい状態となる
- embedding cache が存在すれば高速に復元できるが、無い場合は再計算が発生する
- `_run_id`, `_pair_marker` で run/ペアの同一性を追跡し、誤った状態の使用を防ぐ

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

### 4.1 compare_state を扱うツール群（`compare_app/core/compare_steps.py`）

#### ペアスコープ付きツール（`_build_pair_scoped_compare_tools`）

以下のツールは **ペア指定必須**（`doc_a_id`, `doc_b_id`）で動作し、内部で自動的に状態を復元します:

- `compare_all_chunk_similarity_matching(doc_a_id, doc_b_id, top_k, alpha, beta, min_score)`
  - マッチング実行後、`last_matching` を `work/pairs/{a}_{b}/last_matching.json` へ永続化
- `compare_get_grouping(doc_a_id, doc_b_id, which, mode, ...)`
  - `which="last"` の場合、永続化された `last_matching.json` を優先して読み込む
- `compare_get_chunk(doc_a_id, doc_b_id, which, chunk_id)`
- `compare_specified_chunks_diff(doc_a_id, doc_b_id, chunk_id_a, chunk_id_b, ...)`
- `compare_specified_chunks_llm(doc_a_id, doc_b_id, chunk_id_a, chunk_id_b, focus)`

**重要**: これらのツールは `_ensure_compare_state_for_pair` を呼び出し、必要に応じて `compare_setup` を再実行します（`compare_steps.py:259-336`）。

#### ペア準備ツール（`_build_pair_compare_setup_tool`）

- `pair_compare_setup(doc_a_id, doc_b_id, purpose="")`
  - `compare_setup` + `compare_all_chunk_similarity_matching` を実行
  - `work/pairs/{a}_{b}/` ディレクトリを作成し、`initial_matching.json`, `embedding_cache.json` を保存
  - `doc_hash` が利用可能な場合、`DocumentPairRepository` へも永続化（次run再利用用）

---

## 5. compare_state の既知の課題と改善案

### 5.1 現在の課題

1. **スレッドローカル依存**
   - `COMPARE_STATE` がスレッドローカル辞書のため、マルチスレッド環境で自動復元が必要
   - 復元コストが高い場合（embedding再計算）、パフォーマンスに影響

2. **グローバル状態の管理**
   - PoC由来の設計で、状態がツール間で暗黙的に共有される
   - 複数ペアを同時に扱う場合、状態の切り替えが必要

3. **永続化の二重管理**
   - `work/pairs/` と `document_pairs/` の両方に保存される
   - 同期が必要で、不整合のリスクがある

### 5.2 将来の改善案

1. **状態のコンテキスト化**
   - `COMPARE_STATE` を引数として明示的に渡す設計に移行
   - または `CompareContext` クラスを作成し、ペアごとに独立した状態を管理

2. **キャッシュの統一**
   - `document_pairs/` を唯一の真実の源とし、`work/pairs/` は削除
   - または run 単位の分離を維持しつつ、最終的に `document_pairs/` へマージ

3. **オンデマンド復元の最適化**
   - embedding cache の必須化（存在しない場合はエラー）
   - または、`compare_setup` の結果を run メタデータとしてシリアライズ

---

## 6. 運用メモ（削除/リセット）

既存Runを削除して作り直す（PowerShell例）:

```powershell
Remove-Item -Recurse -Force .\data\runs\*
```

compare_state のデバッグ情報を確認する方法:

```python
from src.tools import COMPARE_STATE
print("run_id:", COMPARE_STATE.get("_run_id"))
print("pair_marker:", COMPARE_STATE.get("_pair_marker"))
print("has initial_matching:", bool(COMPARE_STATE.get("initial_matching")))
print("has ast_a:", bool(COMPARE_STATE.get("ast_a")))
```
