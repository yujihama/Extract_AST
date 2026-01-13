# ドキュメント中心アーキテクチャ設計

## 1. 概要

従来のRun中心のデータ管理から、ドキュメント中心のアーキテクチャに移行する。
入力ファイル、AST、Blueprintはドキュメント単位で管理し、複数のRunで再利用可能にする。

### 1.1 背景・課題

- 同じファイルペアで複数Runを実行すると、AST/Blueprint/embeddingが重複保存される
- `reuse_artifacts_from`でコピーしているが、ストレージが無駄
- ファイル単位での履歴管理ができない

### 1.2 目標

- ドキュメント（入力ファイル）を独立したエンティティとして管理
- AST/Blueprintをドキュメントに紐づけ、自動再利用
- Runは分析結果のみを保持

---

## 2. 新ディレクトリ構造

```
data/
  ├── documents/                    # ドキュメント単位で管理
  │   └── {doc_hash}/               # SHA256の先頭16文字
  │       ├── raw.txt               # 元ファイル（正規化済み）
  │       ├── ast.json              # AST（不変）
  │       ├── blueprint.json        # Blueprint（不変）
  │       └── metadata.json         # メタデータ
  │
  ├── document_pairs/               # ペア単位で管理
  │   └── {pair_hash}/              # doc_a_hash + doc_b_hash から生成
  │       ├── initial_matching.json
  │       └── embedding_cache.json
  │
  └── runs/                         # 分析結果のみ
      └── {run_id}/
          ├── config.json           # 実行設定（doc_a_hash, doc_b_hash, params）
          ├── work/                 # 作業ディレクトリ（pre_analysis結果等）
          │   ├── pre_analysis.json
          │   └── .skip_compare_analysis  # スキップフラグ
          ├── output/               # 最終成果物
          │   ├── template_draft.md
          │   └── template_filled.md
          └── events.jsonl          # イベントログ
```

---

## 3. データモデル

### 3.1 Document

```python
@dataclass
class Document:
    doc_hash: str              # SHA256先頭16文字
    original_filename: str     # 元のファイル名
    content_hash: str          # 全体のSHA256（重複検出用）
    char_count: int            # 文字数
    created_at: datetime
    has_ast: bool              # AST生成済みか
    has_blueprint: bool        # Blueprint生成済みか

class DocumentMetadata(BaseModel):
    """metadata.json のスキーマ"""
    doc_hash: str
    original_filename: str
    content_hash: str
    char_count: int
    created_at: str
    ast_created_at: Optional[str] = None
    blueprint_created_at: Optional[str] = None
```

### 3.2 DocumentPair

```python
@dataclass
class DocumentPair:
    pair_hash: str             # doc_a_hash + "_" + doc_b_hash のハッシュ
    doc_a_hash: str
    doc_b_hash: str
    has_matching: bool         # initial_matching生成済みか
    has_embedding_cache: bool  # embedding_cache生成済みか
```

### 3.3 Run（変更後）

```python
@dataclass
class Run:
    run_id: str
    doc_a_hash: str            # 旧: doc_a_path
    doc_b_hash: str            # 旧: doc_b_path
    status: str
    mode: str
    params: dict               # comparison_focus, steps等
    created_at: datetime
    updated_at: datetime
```

---

## 4. コンポーネント設計

### 4.1 DocumentRepository（新規）

```python
class DocumentRepository:
    """ドキュメントの永続化を管理"""
    
    def __init__(self, base_dir: Path = Path("data/documents")):
        self.base_dir = base_dir
    
    def compute_hash(self, content: bytes) -> str:
        """ファイル内容からハッシュを計算"""
        return hashlib.sha256(content).hexdigest()[:16]
    
    def exists(self, doc_hash: str) -> bool:
        """ドキュメントが存在するか"""
        return (self.base_dir / doc_hash / "raw.txt").exists()
    
    def add(self, content: bytes, original_filename: str) -> Document:
        """ドキュメントを追加（既存なら既存を返す）"""
        doc_hash = self.compute_hash(content)
        if self.exists(doc_hash):
            return self.get(doc_hash)
        # 新規作成
        ...
    
    def get(self, doc_hash: str) -> Optional[Document]:
        """ドキュメントを取得"""
        ...
    
    def list_all(self) -> List[Document]:
        """全ドキュメントを一覧"""
        ...
    
    def get_ast(self, doc_hash: str) -> Optional[dict]:
        """ASTを取得"""
        ...
    
    def save_ast(self, doc_hash: str, ast_data: dict) -> None:
        """ASTを保存"""
        ...
    
    def get_blueprint(self, doc_hash: str) -> Optional[dict]:
        """Blueprintを取得"""
        ...
    
    def save_blueprint(self, doc_hash: str, blueprint_data: dict) -> None:
        """Blueprintを保存"""
        ...
```

### 4.2 DocumentPairRepository（新規）

```python
class DocumentPairRepository:
    """ドキュメントペアの成果物を管理"""
    
    def __init__(self, base_dir: Path = Path("data/document_pairs")):
        self.base_dir = base_dir
    
    def compute_pair_hash(self, doc_a_hash: str, doc_b_hash: str) -> str:
        """ペアハッシュを計算"""
        combined = f"{doc_a_hash}_{doc_b_hash}"
        return hashlib.sha256(combined.encode()).hexdigest()[:16]
    
    def get_or_create(self, doc_a_hash: str, doc_b_hash: str) -> DocumentPair:
        """ペアを取得または作成"""
        ...
    
    def get_matching(self, pair_hash: str) -> Optional[dict]:
        """initial_matchingを取得"""
        ...
    
    def save_matching(self, pair_hash: str, matching_data: dict) -> None:
        """initial_matchingを保存"""
        ...
    
    def get_embedding_cache(self, pair_hash: str) -> Optional[dict]:
        """embedding_cacheを取得"""
        ...
    
    def save_embedding_cache(self, pair_hash: str, cache_data: dict) -> None:
        """embedding_cacheを保存"""
        ...
```

### 4.3 ArtifactStore（変更）

```python
class ArtifactStore:
    """Run単位の成果物管理（分析結果のみ）"""
    
    def __init__(
        self,
        runs_dir: Path = Path("data/runs"),
        doc_repo: DocumentRepository = None,
        pair_repo: DocumentPairRepository = None,
    ):
        self.runs_dir = runs_dir
        self.doc_repo = doc_repo or DocumentRepository()
        self.pair_repo = pair_repo or DocumentPairRepository()
    
    # ドキュメント関連はdoc_repoに委譲
    def get_document(self, doc_hash: str) -> Document:
        return self.doc_repo.get(doc_hash)
    
    def get_ast(self, doc_hash: str) -> Optional[dict]:
        return self.doc_repo.get_ast(doc_hash)
    
    # ペア関連はpair_repoに委譲
    def get_matching(self, doc_a_hash: str, doc_b_hash: str) -> Optional[dict]:
        pair_hash = self.pair_repo.compute_pair_hash(doc_a_hash, doc_b_hash)
        return self.pair_repo.get_matching(pair_hash)
    
    # Run固有の成果物はそのまま
    def get_work_artifact(self, run_id: str, name: str) -> Optional[bytes]:
        ...
    
    def save_work_artifact(self, run_id: str, name: str, data: bytes) -> None:
        ...
```

### 4.4 RunExecutor（変更）

```python
class RunExecutor:
    def create_run(
        self,
        doc_a: Union[str, bytes, Path],  # ファイルパス、バイナリ、またはdoc_hash
        doc_b: Union[str, bytes, Path],
        params: dict = None,
    ) -> Run:
        """
        Runを作成
        - doc_a/doc_bがパスまたはバイナリの場合: DocumentRepositoryに追加
        - doc_a/doc_bがdoc_hashの場合: 既存ドキュメントを参照
        """
        # ドキュメントを解決
        doc_a_hash = self._resolve_document(doc_a)
        doc_b_hash = self._resolve_document(doc_b)
        
        # Runを作成
        run_id = uuid.uuid4().hex
        run = Run(
            run_id=run_id,
            doc_a_hash=doc_a_hash,
            doc_b_hash=doc_b_hash,
            status="created",
            ...
        )
        ...
```

---

## 5. Pipeline Step変更

### 5.1 CompareSetupStep

```python
class CompareSetupStep(Step):
    def run(self, ctx: StepContext) -> None:
        doc_a_hash = ctx.params["doc_a_hash"]
        doc_b_hash = ctx.params["doc_b_hash"]
        
        # ASTが存在するかチェック
        ast_a = self.doc_repo.get_ast(doc_a_hash)
        if ast_a is None:
            # AST生成
            raw_a = self.doc_repo.get_raw(doc_a_hash)
            ast_a = generate_ast(raw_a)
            self.doc_repo.save_ast(doc_a_hash, ast_a)
        
        # Blueprint同様
        ...
        
        # ペア成果物
        pair_hash = self.pair_repo.compute_pair_hash(doc_a_hash, doc_b_hash)
        matching = self.pair_repo.get_matching(pair_hash)
        if matching is None:
            matching = generate_matching(ast_a, ast_b)
            self.pair_repo.save_matching(pair_hash, matching)
        
        # COMPARE_STATEに設定
        ...
```

---

## 6. UI変更

### 6.1 ドキュメント管理画面（新規）

- `/documents` - ドキュメント一覧
- `/documents/{doc_hash}` - ドキュメント詳細（AST、Blueprint表示）

### 6.2 Run作成画面（変更）

```html
<!-- 方法1: 新規ファイルアップロード -->
<input type="file" name="doc_a_file" />

<!-- 方法2: 既存ドキュメントから選択 -->
<select name="doc_a_hash">
  <option value="">新規アップロード</option>
  {% for doc in documents %}
  <option value="{{ doc.doc_hash }}">
    {{ doc.original_filename }} ({{ doc.char_count }}文字)
  </option>
  {% endfor %}
</select>
```

### 6.3 Run詳細画面（変更）

- ドキュメントへのリンク追加
- AST/Blueprint表示をドキュメント画面に移動

---

## 7. 実装計画

### Phase 1: 基盤実装
1. 既存runデータ削除
2. DocumentRepository実装
3. DocumentPairRepository実装
4. ArtifactStore変更

### Phase 2: Pipeline変更
5. compare_steps.pyの変更（AST/Blueprint保存先変更）
6. RunExecutor変更

### Phase 3: UI変更
7. ドキュメント管理画面追加
8. Run作成画面変更
9. Run詳細画面変更

### Phase 4: テスト
10. 統合テスト
11. UI動作確認

---

## 8. マイグレーション

既存データは全削除のため、マイグレーションスクリプトは不要。

```bash
# 既存データ削除
rm -rf data/runs/*
```

---

## 9. API変更

### 9.1 ドキュメントAPI（新規）

```
GET  /api/documents          # 一覧
POST /api/documents          # 新規登録
GET  /api/documents/{hash}   # 詳細
GET  /api/documents/{hash}/ast
GET  /api/documents/{hash}/blueprint
```

### 9.2 Run API（変更）

```
POST /api/runs
{
  "doc_a_hash": "abc123...",     # 既存ドキュメント参照
  "doc_b_hash": "def456...",
  // または
  "doc_a_file": <upload>,        # 新規アップロード
  "doc_b_file": <upload>,
  "mode": "real",
  "params": {...}
}
```
