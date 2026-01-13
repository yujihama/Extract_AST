"""ドキュメント中心アーキテクチャのリポジトリ実装。

ドキュメント（入力ファイル）とドキュメントペアを独立したエンティティとして管理し、
複数のRunで再利用可能にする。
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Document:
    """ドキュメントエンティティ"""
    doc_hash: str
    original_filename: str
    content_hash: str  # 全体SHA256（重複検出用）
    char_count: int
    created_at: datetime
    has_ast: bool = False
    has_blueprint: bool = False
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "doc_hash": self.doc_hash,
            "original_filename": self.original_filename,
            "content_hash": self.content_hash,
            "char_count": self.char_count,
            "created_at": self.created_at.isoformat(),
            "has_ast": self.has_ast,
            "has_blueprint": self.has_blueprint,
        }
    
    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Document":
        created_at = d["created_at"]
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        return cls(
            doc_hash=d["doc_hash"],
            original_filename=d["original_filename"],
            content_hash=d["content_hash"],
            char_count=d["char_count"],
            created_at=created_at,
            has_ast=d.get("has_ast", False),
            has_blueprint=d.get("has_blueprint", False),
        )


@dataclass
class DocumentPair:
    """ドキュメントペアエンティティ"""
    pair_hash: str
    doc_a_hash: str
    doc_b_hash: str
    has_matching: bool = False
    has_embedding_cache: bool = False
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "pair_hash": self.pair_hash,
            "doc_a_hash": self.doc_a_hash,
            "doc_b_hash": self.doc_b_hash,
            "has_matching": self.has_matching,
            "has_embedding_cache": self.has_embedding_cache,
        }
    
    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DocumentPair":
        return cls(
            pair_hash=d["pair_hash"],
            doc_a_hash=d["doc_a_hash"],
            doc_b_hash=d["doc_b_hash"],
            has_matching=d.get("has_matching", False),
            has_embedding_cache=d.get("has_embedding_cache", False),
        )


@dataclass
class DocumentRepository:
    """ドキュメントの永続化を管理"""
    
    base_dir: Path = field(default_factory=lambda: Path("data/documents"))
    
    def __post_init__(self):
        self.base_dir = Path(self.base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
    
    def compute_hash(self, content: bytes) -> str:
        """ファイル内容からハッシュを計算（先頭16文字）"""
        return hashlib.sha256(content).hexdigest()[:16]
    
    def compute_full_hash(self, content: bytes) -> str:
        """ファイル内容から完全なハッシュを計算"""
        return hashlib.sha256(content).hexdigest()
    
    def _doc_dir(self, doc_hash: str) -> Path:
        return self.base_dir / doc_hash
    
    def exists(self, doc_hash: str) -> bool:
        """ドキュメントが存在するか"""
        return (self._doc_dir(doc_hash) / "raw.txt").exists()
    
    def add(self, content: bytes, original_filename: str) -> Document:
        """ドキュメントを追加（既存なら既存を返す）"""
        doc_hash = self.compute_hash(content)
        
        if self.exists(doc_hash):
            return self.get(doc_hash)
        
        # 新規作成
        doc_dir = self._doc_dir(doc_hash)
        doc_dir.mkdir(parents=True, exist_ok=True)
        
        # rawファイルを保存
        raw_path = doc_dir / "raw.txt"
        raw_path.write_bytes(content)
        
        # テキストとして文字数をカウント
        try:
            text = content.decode("utf-8")
            char_count = len(text)
        except UnicodeDecodeError:
            char_count = len(content)
        
        doc = Document(
            doc_hash=doc_hash,
            original_filename=original_filename,
            content_hash=self.compute_full_hash(content),
            char_count=char_count,
            created_at=_utcnow(),
            has_ast=False,
            has_blueprint=False,
        )
        
        # メタデータを保存
        self._save_metadata(doc)
        
        return doc
    
    def add_from_path(self, src_path: str, original_filename: Optional[str] = None) -> Document:
        """ファイルパスからドキュメントを追加"""
        src = Path(src_path)
        content = src.read_bytes()
        filename = original_filename or src.name
        return self.add(content, filename)
    
    def _save_metadata(self, doc: Document) -> None:
        """メタデータを保存"""
        meta_path = self._doc_dir(doc.doc_hash) / "metadata.json"
        meta_path.write_text(json.dumps(doc.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    
    def get(self, doc_hash: str) -> Optional[Document]:
        """ドキュメントを取得"""
        meta_path = self._doc_dir(doc_hash) / "metadata.json"
        if not meta_path.exists():
            return None
        
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        return Document.from_dict(data)
    
    def list_all(self) -> List[Document]:
        """全ドキュメントを一覧"""
        docs = []
        if not self.base_dir.exists():
            return docs
        
        for d in self.base_dir.iterdir():
            if d.is_dir():
                doc = self.get(d.name)
                if doc:
                    docs.append(doc)
        
        # 作成日時の降順でソート
        docs.sort(key=lambda x: x.created_at, reverse=True)
        return docs
    
    def get_raw(self, doc_hash: str) -> Optional[bytes]:
        """生ファイルを取得"""
        raw_path = self._doc_dir(doc_hash) / "raw.txt"
        if not raw_path.exists():
            return None
        return raw_path.read_bytes()
    
    def get_raw_text(self, doc_hash: str) -> Optional[str]:
        """生ファイルをテキストとして取得"""
        content = self.get_raw(doc_hash)
        if content is None:
            return None
        return content.decode("utf-8", errors="replace")
    
    def get_raw_path(self, doc_hash: str) -> Optional[Path]:
        """生ファイルのパスを取得"""
        raw_path = self._doc_dir(doc_hash) / "raw.txt"
        if raw_path.exists():
            return raw_path
        return None
    
    def get_ast(self, doc_hash: str) -> Optional[dict]:
        """ASTを取得"""
        ast_path = self._doc_dir(doc_hash) / "ast.json"
        if not ast_path.exists():
            return None
        return json.loads(ast_path.read_text(encoding="utf-8"))
    
    def save_ast(self, doc_hash: str, ast_data: dict) -> None:
        """ASTを保存"""
        doc = self.get(doc_hash)
        if not doc:
            raise ValueError(f"Document not found: {doc_hash}")
        
        ast_path = self._doc_dir(doc_hash) / "ast.json"
        ast_path.write_text(json.dumps(ast_data, ensure_ascii=False, indent=2), encoding="utf-8")
        
        # メタデータを更新
        doc.has_ast = True
        self._save_metadata(doc)
    
    def get_blueprint(self, doc_hash: str) -> Optional[dict]:
        """Blueprintを取得"""
        bp_path = self._doc_dir(doc_hash) / "blueprint.json"
        if not bp_path.exists():
            return None
        return json.loads(bp_path.read_text(encoding="utf-8"))
    
    def save_blueprint(self, doc_hash: str, blueprint_data: dict) -> None:
        """Blueprintを保存"""
        doc = self.get(doc_hash)
        if not doc:
            raise ValueError(f"Document not found: {doc_hash}")
        
        bp_path = self._doc_dir(doc_hash) / "blueprint.json"
        bp_path.write_text(json.dumps(blueprint_data, ensure_ascii=False, indent=2), encoding="utf-8")
        
        # メタデータを更新
        doc.has_blueprint = True
        self._save_metadata(doc)
    
    def delete(self, doc_hash: str) -> bool:
        """ドキュメントを削除（ディレクトリごと削除）
        
        Returns:
            削除成功ならTrue、存在しなければFalse
        """
        import shutil
        
        doc_dir = self._doc_dir(doc_hash)
        if not doc_dir.exists():
            return False
        
        shutil.rmtree(doc_dir)
        return True


@dataclass
class DocumentPairRepository:
    """ドキュメントペアの成果物を管理"""
    
    base_dir: Path = field(default_factory=lambda: Path("data/document_pairs"))
    
    def __post_init__(self):
        self.base_dir = Path(self.base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
    
    def compute_pair_hash(self, doc_a_hash: str, doc_b_hash: str) -> str:
        """ペアハッシュを計算"""
        combined = f"{doc_a_hash}_{doc_b_hash}"
        return hashlib.sha256(combined.encode()).hexdigest()[:16]
    
    def _pair_dir(self, pair_hash: str) -> Path:
        return self.base_dir / pair_hash
    
    def _meta_path(self, pair_hash: str) -> Path:
        return self._pair_dir(pair_hash) / "metadata.json"
    
    def exists(self, doc_a_hash: str, doc_b_hash: str) -> bool:
        """ペアが存在するか"""
        pair_hash = self.compute_pair_hash(doc_a_hash, doc_b_hash)
        return self._meta_path(pair_hash).exists()
    
    def get_or_create(self, doc_a_hash: str, doc_b_hash: str) -> DocumentPair:
        """ペアを取得または作成"""
        pair_hash = self.compute_pair_hash(doc_a_hash, doc_b_hash)
        pair_dir = self._pair_dir(pair_hash)
        meta_path = self._meta_path(pair_hash)
        
        if meta_path.exists():
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            return DocumentPair.from_dict(data)
        
        # 新規作成
        pair_dir.mkdir(parents=True, exist_ok=True)
        pair = DocumentPair(
            pair_hash=pair_hash,
            doc_a_hash=doc_a_hash,
            doc_b_hash=doc_b_hash,
            has_matching=False,
            has_embedding_cache=False,
        )
        meta_path.write_text(json.dumps(pair.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return pair
    
    def _save_metadata(self, pair: DocumentPair) -> None:
        """メタデータを保存"""
        meta_path = self._meta_path(pair.pair_hash)
        meta_path.write_text(json.dumps(pair.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    
    def get(self, pair_hash: str) -> Optional[DocumentPair]:
        """ペアを取得"""
        meta_path = self._meta_path(pair_hash)
        if not meta_path.exists():
            return None
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        return DocumentPair.from_dict(data)
    
    def get_matching(self, doc_a_hash: str, doc_b_hash: str) -> Optional[dict]:
        """initial_matchingを取得"""
        pair_hash = self.compute_pair_hash(doc_a_hash, doc_b_hash)
        matching_path = self._pair_dir(pair_hash) / "initial_matching.json"
        if not matching_path.exists():
            return None
        return json.loads(matching_path.read_text(encoding="utf-8"))
    
    def save_matching(self, doc_a_hash: str, doc_b_hash: str, matching_data: dict) -> None:
        """initial_matchingを保存"""
        pair = self.get_or_create(doc_a_hash, doc_b_hash)
        matching_path = self._pair_dir(pair.pair_hash) / "initial_matching.json"
        matching_path.write_text(json.dumps(matching_data, ensure_ascii=False, indent=2), encoding="utf-8")
        
        # メタデータを更新
        pair.has_matching = True
        self._save_metadata(pair)
    
    def get_embedding_cache(self, doc_a_hash: str, doc_b_hash: str) -> Optional[dict]:
        """embedding_cacheを取得"""
        pair_hash = self.compute_pair_hash(doc_a_hash, doc_b_hash)
        cache_path = self._pair_dir(pair_hash) / "embedding_cache.json"
        if not cache_path.exists():
            return None
        return json.loads(cache_path.read_text(encoding="utf-8"))
    
    def save_embedding_cache(self, doc_a_hash: str, doc_b_hash: str, cache_data: dict) -> None:
        """embedding_cacheを保存"""
        pair = self.get_or_create(doc_a_hash, doc_b_hash)
        cache_path = self._pair_dir(pair.pair_hash) / "embedding_cache.json"
        cache_path.write_text(json.dumps(cache_data, ensure_ascii=False, indent=2), encoding="utf-8")
        
        # メタデータを更新
        pair.has_embedding_cache = True
        self._save_metadata(pair)
    
    def list_all(self) -> List[DocumentPair]:
        """全ペアを一覧"""
        pairs = []
        if not self.base_dir.exists():
            return pairs
        
        for d in self.base_dir.iterdir():
            if d.is_dir():
                pair = self.get(d.name)
                if pair:
                    pairs.append(pair)
        
        return pairs
