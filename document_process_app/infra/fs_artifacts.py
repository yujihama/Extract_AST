"""ドキュメント中心アーキテクチャ対応のArtifactStore。

Run単位の成果物管理に加え、DocumentRepository/DocumentPairRepositoryと連携して
ドキュメント・ペア単位の成果物を管理する。
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from document_process_app.infra.document_store import (
    Document,
    DocumentPair,
    DocumentPairRepository,
    DocumentRepository,
)


@dataclass
class FileArtifactStore:
    """ドキュメント中心アーキテクチャ対応のArtifactStore。

    - ドキュメント（入力ファイル）はDocumentRepositoryで管理
    - ペア成果物（matching, embedding_cache）はDocumentPairRepositoryで管理
    - Run固有の成果物（分析結果等）はrun_dir配下に配置
    """

    runs_root: Path = field(default_factory=lambda: Path("data") / "runs")
    doc_repo: DocumentRepository = field(default_factory=DocumentRepository)
    pair_repo: DocumentPairRepository = field(default_factory=DocumentPairRepository)

    def __post_init__(self):
        self.runs_root = Path(self.runs_root)
        self.runs_root.mkdir(parents=True, exist_ok=True)

    def ensure_run_dirs(self, run_id: str) -> Dict[str, str]:
        """Run用のディレクトリを作成"""
        run_dir = self.runs_root / run_id
        paths = {
            "run_dir": str(run_dir),
            "input_dir": str(run_dir / "input"),  # run入力の配置先
            "work_dir": str(run_dir / "work"),
            "out_dir": str(run_dir / "out"),
            "log_dir": str(run_dir / "log"),
            "cache_dir": str(run_dir / "cache"),  # run単位キャッシュ
        }
        for p in paths.values():
            os.makedirs(p, exist_ok=True)
        return paths

    def add_input(self, run_id: str, *, which: str, src_path: str) -> Document:
        """入力ファイルをDocumentRepositoryに追加し、Runと紐付ける。
        
        Args:
            run_id: Run ID
            which: "a" or "b"
            src_path: 入力ファイルのパス
            
        Returns:
            追加されたDocument
        """
        import shutil
        
        src = Path(src_path)
        if not src.exists():
            raise FileNotFoundError(str(src))
        
        # DocumentRepositoryに追加（既存なら既存を返す）
        doc = self.doc_repo.add_from_path(str(src), src.name)
        
        # Runのconfigに紐付けを記録
        self._update_run_config(
            run_id,
            which,
            doc.doc_hash,
            original_filename=doc.original_filename,
            source={"type": "path", "path": str(src)},
        )
        
        # input_dirにもコピー（既存ステップが参照するため）
        paths = self.ensure_run_dirs(run_id)
        input_dir = Path(paths["input_dir"])
        dest_name = f"doc_{which}{src.suffix}"
        dest = input_dir / dest_name
        raw_path = self.doc_repo.get_raw_path(doc.doc_hash)
        if raw_path and raw_path.exists():
            shutil.copy2(raw_path, dest)

        # 既存のAST/Blueprintがあればwork_dirにコピー（次runでも要約済みASTを確実に使うため）
        # add_input_by_hash と同様の挙動に揃える（パス指定でもdoc_hashが同一なら再利用する）
        work_dir = Path(paths["work_dir"])
        doc_dir = self.doc_repo.base_dir / doc.doc_hash

        # Blueprint
        bp_src = doc_dir / "blueprint.json"
        bp_dest = work_dir / f"blueprint_{which}.json"
        if bp_src.exists() and not bp_dest.exists():
            try:
                shutil.copy2(bp_src, bp_dest)
            except Exception:
                pass

        # AST（要約済みcontent_summaryを含む可能性がある）
        ast_src = doc_dir / "ast.json"
        ast_dest = work_dir / f"ast_{which}.ast.json"
        if ast_src.exists() and not ast_dest.exists():
            try:
                shutil.copy2(ast_src, ast_dest)
            except Exception:
                pass
        
        return doc

    def add_input_by_hash(self, run_id: str, *, which: str, doc_hash: str) -> Document:
        """既存ドキュメントをRunに紐付ける。
        
        Args:
            run_id: Run ID
            which: "a" or "b"
            doc_hash: 既存ドキュメントのハッシュ
            
        Returns:
            ドキュメント
        """
        import shutil
        
        doc = self.doc_repo.get(doc_hash)
        if not doc:
            raise ValueError(f"Document not found: {doc_hash}")
        
        # Runのconfigに紐付けを記録
        self._update_run_config(
            run_id,
            which,
            doc_hash,
            original_filename=doc.original_filename,
            source={"type": "hash"},
        )
        
        paths = self.ensure_run_dirs(run_id)
        input_dir = Path(paths["input_dir"])
        work_dir = Path(paths["work_dir"])
        
        # input_dirにもコピー（既存ステップが参照するため）
        original_ext = Path(doc.original_filename).suffix or ".txt"
        dest_name = f"doc_{which}{original_ext}"
        dest = input_dir / dest_name
        raw_path = self.doc_repo.get_raw_path(doc_hash)
        if raw_path and raw_path.exists() and not dest.exists():
            shutil.copy2(raw_path, dest)
        
        # 既存のAST/Blueprintがあればwork_dirにコピー（ステップスキップ用）
        doc_dir = self.doc_repo.base_dir / doc_hash
        
        # Blueprint
        bp_src = doc_dir / "blueprint.json"
        bp_dest = work_dir / f"blueprint_{which}.json"
        if bp_src.exists() and not bp_dest.exists():
            shutil.copy2(bp_src, bp_dest)
        
        # AST
        ast_src = doc_dir / "ast.json"
        ast_dest = work_dir / f"ast_{which}.ast.json"
        if ast_src.exists() and not ast_dest.exists():
            shutil.copy2(ast_src, ast_dest)
        
        return doc

    def _update_run_config(
        self,
        run_id: str,
        which: str,
        doc_hash: str,
        *,
        original_filename: Optional[str] = None,
        source: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Runのconfigを更新"""
        paths = self.ensure_run_dirs(run_id)
        config_path = Path(paths["run_dir"]) / "config.json"
        
        if config_path.exists():
            config = json.loads(config_path.read_text(encoding="utf-8"))
        else:
            config = {}
        
        doc_id = str(which)
        config[f"doc_{doc_id}_hash"] = doc_hash

        docs = config.get("documents")
        if not isinstance(docs, list):
            docs = []

        entry: Dict[str, Any] = {"doc_id": doc_id, "doc_hash": doc_hash}
        if original_filename:
            entry["filename"] = str(original_filename)
        if source:
            entry["source"] = dict(source)

        for i, existing in enumerate(docs):
            if isinstance(existing, dict) and existing.get("doc_id") == doc_id:
                docs[i] = {**existing, **entry}
                break
        else:
            docs.append(entry)
        config["documents"] = docs
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    def get_run_config(self, run_id: str) -> Dict[str, Any]:
        """Runのconfigを取得"""
        config_path = self.runs_root / run_id / "config.json"
        if not config_path.exists():
            return {}
        return json.loads(config_path.read_text(encoding="utf-8"))

    def save_run_config(self, run_id: str, config: Dict[str, Any]) -> None:
        """Runのconfigを保存"""
        paths = self.ensure_run_dirs(run_id)
        config_path = Path(paths["run_dir"]) / "config.json"
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    # --- ドキュメント関連（doc_repoに委譲） ---
    
    def get_document(self, doc_hash: str) -> Optional[Document]:
        """ドキュメントを取得"""
        return self.doc_repo.get(doc_hash)
    
    def list_documents(self):
        """全ドキュメントを一覧"""
        return self.doc_repo.list_all()
    
    def get_raw_text(self, doc_hash: str) -> Optional[str]:
        """生テキストを取得"""
        return self.doc_repo.get_raw_text(doc_hash)
    
    def get_raw_path(self, doc_hash: str) -> Optional[Path]:
        """生ファイルのパスを取得"""
        return self.doc_repo.get_raw_path(doc_hash)
    
    def get_ast(self, doc_hash: str) -> Optional[dict]:
        """ASTを取得"""
        return self.doc_repo.get_ast(doc_hash)
    
    def save_ast(self, doc_hash: str, ast_data: dict) -> None:
        """ASTを保存"""
        self.doc_repo.save_ast(doc_hash, ast_data)
    
    def get_blueprint(self, doc_hash: str) -> Optional[dict]:
        """Blueprintを取得"""
        return self.doc_repo.get_blueprint(doc_hash)
    
    def save_blueprint(self, doc_hash: str, blueprint_data: dict) -> None:
        """Blueprintを保存"""
        self.doc_repo.save_blueprint(doc_hash, blueprint_data)

    # --- ペア関連（pair_repoに委譲） ---
    
    def get_pair(self, doc_a_hash: str, doc_b_hash: str) -> DocumentPair:
        """ペアを取得または作成"""
        return self.pair_repo.get_or_create(doc_a_hash, doc_b_hash)
    
    def get_matching(self, doc_a_hash: str, doc_b_hash: str) -> Optional[dict]:
        """initial_matchingを取得"""
        return self.pair_repo.get_matching(doc_a_hash, doc_b_hash)
    
    def save_matching(self, doc_a_hash: str, doc_b_hash: str, matching_data: dict) -> None:
        """initial_matchingを保存"""
        self.pair_repo.save_matching(doc_a_hash, doc_b_hash, matching_data)
    
    def get_embedding_cache(self, doc_a_hash: str, doc_b_hash: str) -> Optional[dict]:
        """embedding_cacheを取得"""
        return self.pair_repo.get_embedding_cache(doc_a_hash, doc_b_hash)
    
    def save_embedding_cache(self, doc_a_hash: str, doc_b_hash: str, cache_data: dict) -> None:
        """embedding_cacheを保存"""
        self.pair_repo.save_embedding_cache(doc_a_hash, doc_b_hash, cache_data)

    # --- Run固有の成果物 ---
    
    def get_work_path(self, run_id: str, name: str) -> Path:
        """work配下のファイルパスを取得"""
        return self.runs_root / run_id / "work" / name
    
    def get_out_path(self, run_id: str, name: str) -> Path:
        """out配下のファイルパスを取得"""
        return self.runs_root / run_id / "out" / name
    
    def save_work_artifact(self, run_id: str, name: str, data: bytes) -> Path:
        """work配下に成果物を保存"""
        paths = self.ensure_run_dirs(run_id)
        path = Path(paths["work_dir"]) / name
        path.write_bytes(data)
        return path
    
    def save_out_artifact(self, run_id: str, name: str, data: bytes) -> Path:
        """out配下に成果物を保存"""
        paths = self.ensure_run_dirs(run_id)
        path = Path(paths["out_dir"]) / name
        path.write_bytes(data)
        return path

