# ast_store_tool（AST永続化ツール）

長文ドキュメントの解析で「最後にASTを書き出す」方式だと、LLMの会話履歴要約/コンテキスト制限の影響で**後半しか残らない**問題が起きやすいです。  
`ast_store` は、ASTを**JSONファイルに都度保存**し、必要なときに**現在のASTを読み込み・変更・追記**できるようにするためのLangChain Toolです。

さらに `test.ipynb` 版は、階層誤り（親パスの取り違え）を減らすために以下を実装しています:
- **タイトルパス（`parent_titles` / `node_titles`）で操作**できる
- **書き込み前に `load_meta` を必須化**し、そこで発行される **ワンタイム `edit_token`** が無いと append/update できない  
  → 「現状ASTを見ずに盲目的に追記してズレる」を強制的に防ぎます

## 使い方（Python / Notebook）

補足:
- `test.ipynb` は **`## 1. Custom Tools Setup`（セル4）内に `ast_store` を内蔵**しているため、ノートブック単体で完結します（推奨）。
- `ast_store_tool.py` はスクリプト向けの外だし版です（Notebook版と差が出る可能性があるため、必要なら合わせて更新してください）。

```python
# Notebook内蔵版を想定（test.ipynbのセル4に定義済み）
import json

ast_path = "my_doc.ast.json"

# 初期化（新規作成）
ast_store.invoke({
  "action": "init",
  "ast_path": ast_path,
  "file_name": "my_doc.txt",
  "root_summary": "Document root"
})

# 例: ルート直下に「第1章」を作る（write前にload_metaが必須）
meta = json.loads(ast_store.invoke({
  "action": "load_meta",
  "ast_path": ast_path,
  "purpose": "append_child",
  "node_titles": []  # root
}))

ast_store.invoke({
  "action": "append_child_by_titles",
  "ast_path": ast_path,
  "parent_titles": [],
  "section_title": "第1章",
  "content_summary": "概要…",
  "edit_token": meta["edit_token"]
})

# 子ノードを追加（親をタイトルで指定）
meta = json.loads(ast_store.invoke({
  "action": "load_meta",
  "ast_path": ast_path,
  "purpose": "append_child",
  "node_titles": ["第1章"]  # parent
}))
ast_store.invoke({
  "action": "append_child_by_titles",
  "ast_path": ast_path,
  "parent_titles": ["第1章"],
  "section_title": "1.1 節",
  "content_summary": "…",
  "edit_token": meta["edit_token"]
})

# 現状確認
ast_store.invoke({"action": "list_children", "ast_path": ast_path, "node_titles": ["第1章"]})
```

## ASTパス（node_path / parent_path）

- `[]` はルートノードを指します  
- `[0]` は「ルート直下の1番目の子」  
- `[0, 2]` は「ルート直下1番目の子の、3番目の子」  

## action一覧

- `init`: ASTファイルを新規作成（上書き）
- `load`: AST全体を返す（大きくなり得る）
- `load_subtree`: 指定ノード（部分木）だけ返す（トークン節約）
- `load_meta`: **書き込み前に必須**。対象ノードの現状（children等）とワンタイム `edit_token` を返す
- `list_children`: 指定ノード直下の子タイトル一覧（インデックス付き）
- `resolve_path`: タイトルパスから `node_path` を解決
- `append_child_by_titles`: 親をタイトルパスで指定して子ノード追加（`edit_token`必須）
- `upsert_child_by_titles`: 親をタイトルパスで指定して同名子があれば追記/無ければ作成（`edit_token`必須）
- `update_node_by_titles`: タイトルパスでノード特定して上書き更新（`edit_token`必須）
- `append_to_summary_by_titles`: タイトルパスでノード特定してサマリ追記（`edit_token`必須）
- `append_child` / `upsert_child_by_title` / `update_node` / `append_to_summary`: 旧来のindex指定版（`edit_token`必須）
- `find_by_title`: `section_title` の部分一致でノード検索（パスを返す）


