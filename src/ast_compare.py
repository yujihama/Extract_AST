from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple


def _load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _dump_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


@contextlib.contextmanager
def _exclusive_file_lock(
    lock_path: str,
    *,
    timeout_seconds: float = 30.0,
    poll_interval_seconds: float = 0.05,
):
    """
    プロセス間/スレッド間で使える簡易ファイルロック。

    Windows: msvcrt.locking を使用（ノンブロッキングでリトライ）
    Unix:    fcntl.flock を使用

    - lock_path が空ならロックしない（nullcontext）
    - ロックファイル自体は `.lock` 等を想定
    """
    if not lock_path:
        yield
        return

    parent = os.path.dirname(os.path.abspath(lock_path))
    if parent:
        os.makedirs(parent, exist_ok=True)

    # バイナリで開き、1byte をロックする（EOFでもロックできるように1byte確保）
    f = open(lock_path, "a+b")
    try:
        f.seek(0, os.SEEK_END)
        if f.tell() == 0:
            try:
                f.write(b"\0")
                f.flush()
            except Exception:
                # 書けなくてもロック自体は試す
                pass
        f.seek(0)

        start = time.time()
        is_windows = os.name == "nt"
        if is_windows:
            import msvcrt  # type: ignore

            def _try_lock() -> None:
                # 1byte を非ブロッキングでロック
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)

            def _unlock() -> None:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)

        else:
            import fcntl  # type: ignore

            def _try_lock() -> None:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            def _unlock() -> None:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

        while True:
            try:
                _try_lock()
                break
            except OSError:
                if (time.time() - start) >= float(timeout_seconds):
                    raise TimeoutError(f"Failed to acquire file lock within {timeout_seconds}s: {lock_path}")
                time.sleep(float(poll_interval_seconds))

        try:
            yield
        finally:
            try:
                _unlock()
            except Exception:
                pass
    finally:
        try:
            f.close()
        except Exception:
            pass


def _atomic_write_text(path: str, text: str, encoding: str = "utf-8") -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)

    # 同時実行時に tmp名が衝突しないように、同一ディレクトリに一意な tmp を作る
    fd, tmp_path = tempfile.mkstemp(
        prefix=f"{os.path.basename(path)}.tmp.",
        suffix=".json",
        dir=parent or None,
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as f:
            f.write(text)
            try:
                f.flush()
                os.fsync(f.fileno())
            except Exception:
                # fsync できない環境でも書き込みは継続
                pass

        # Windows は他スレッド/他プロセスの一瞬のオープンでも replace が失敗し得るのでリトライ
        last_err: Optional[BaseException] = None
        for attempt in range(0, 30):
            try:
                os.replace(tmp_path, path)
                return
            except PermissionError as e:
                last_err = e
                # WinError 32 相当は少し待てば解消することが多い
                time.sleep(0.02 * float(attempt + 1))

        # ここまで来たら諦めて例外を投げる
        if last_err is not None:
            raise last_err
        os.replace(tmp_path, path)
    finally:
        # replace に失敗した場合のみ残るので掃除
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


def _normalize_text(s: str) -> str:
    # 全角スペース→半角、連続空白→1つ
    t = (s or "").replace("\u3000", " ")
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _iter_nodes(root: Dict[str, Any], path: Optional[List[int]] = None):
    """Yield (node, path) preorder. path is indices from root.children."""
    if path is None:
        path = []
    yield root, path
    children = root.get("children") or []
    if not isinstance(children, list):
        return
    for i, ch in enumerate(children):
        if not isinstance(ch, dict):
            continue
        yield from _iter_nodes(ch, path + [i])


def _is_leaf(node: Dict[str, Any]) -> bool:
    children = node.get("children") or []
    return not (isinstance(children, list) and any(isinstance(c, dict) for c in children))


def _collect_subtree_content(node: Dict[str, Any]) -> str:
    """ノード配下（自身を含む）のすべての content を再帰的に結合して返す。"""
    parts: List[str] = []
    content = node.get("content")
    if isinstance(content, str) and content.strip():
        parts.append(content.strip())
    children = node.get("children") or []
    if isinstance(children, list):
        for ch in children:
            if isinstance(ch, dict):
                sub = _collect_subtree_content(ch)
                if sub:
                    parts.append(sub)
    return "\n\n".join(parts)


def _collect_subtree_content_with_titles(
    node: Dict[str, Any],
    *,
    max_chars_per_content: int = 0,
    chunk_id_for_reference: str = "",
) -> str:
    """
    ノード配下（自身を含む）のすべての content をセクションタイトル付きで再帰的に結合して返す。
    
    Args:
        node: 対象ノード
        max_chars_per_content: 各contentの最大文字数（0以下は無制限）
        chunk_id_for_reference: 切り詰め時の参照用チャンクID
    """
    parts: List[str] = []
    
    def walk(n: Dict[str, Any], depth: int = 0) -> None:
        # セクションタイトルを取得
        title = n.get("section_title")
        title_str = title.strip() if isinstance(title, str) else ""
        
        # contentを取得
        content = n.get("content")
        content_str = content.strip() if isinstance(content, str) else ""
        
        # タイトルとcontentを追加
        if title_str or content_str:
            section_parts: List[str] = []
            if title_str:
                # 深さに応じた見出しレベル
                prefix = "#" * min(depth + 1, 4)
                section_parts.append(f"{prefix} {title_str}")
            if content_str:
                # 文字数制限
                if max_chars_per_content > 0 and len(content_str) > max_chars_per_content:
                    truncated = content_str[:max_chars_per_content].rstrip()
                    ref_msg = f"※全文を参照する場合はチャンク {chunk_id_for_reference} を直接参照してください" if chunk_id_for_reference else "※全文は省略されています"
                    section_parts.append(f"{truncated}\n...(truncated) {ref_msg}")
                else:
                    section_parts.append(content_str)
            if section_parts:
                parts.append("\n".join(section_parts))
        
        # 子ノードを再帰処理
        children = n.get("children") or []
        if isinstance(children, list):
            for ch in children:
                if isinstance(ch, dict):
                    walk(ch, depth + 1)
    
    walk(node, 0)
    return "\n\n".join(parts)


def _collect_subtree_content_with_summaries(
    node: Dict[str, Any],
    *,
    max_chars_per_content: int = 0,
    chunk_id_for_reference: str = "",
) -> str:
    """
    ノード配下（自身を含む）のすべての content_summary と content を
    セクションタイトル付きで再帰的に結合して返す（LLM比較用）。
    
    Args:
        node: 対象ノード
        max_chars_per_content: 各contentの最大文字数（0以下は無制限）
        chunk_id_for_reference: 切り詰め時の参照用チャンクID
    """
    parts: List[str] = []
    
    def walk(n: Dict[str, Any], depth: int = 0) -> None:
        # セクションタイトルを取得
        title = n.get("section_title")
        title_str = title.strip() if isinstance(title, str) else ""
        
        # content_summaryを取得
        summary = n.get("content_summary")
        summary_str = summary.strip() if isinstance(summary, str) else ""
        
        # contentを取得
        content = n.get("content")
        content_str = content.strip() if isinstance(content, str) else ""
        
        # タイトル、summary、contentを追加
        if title_str or summary_str or content_str:
            section_parts: List[str] = []
            if title_str:
                prefix = "#" * min(depth + 1, 4)
                section_parts.append(f"{prefix} {title_str}")
            if summary_str:
                section_parts.append(f"[コンテキスト]: {summary_str}")
            if content_str:
                # 文字数制限
                if max_chars_per_content > 0 and len(content_str) > max_chars_per_content:
                    truncated = content_str[:max_chars_per_content].rstrip()
                    ref_msg = f"※全文を参照する場合はチャンク {chunk_id_for_reference} を直接参照してください" if chunk_id_for_reference else "※全文は省略されています"
                    section_parts.append(f"[比較対象の文章]: {truncated}\n...(truncated) {ref_msg}")
                else:
                    section_parts.append(f"[比較対象の文章]: {content_str}")
            if section_parts:
                parts.append("\n".join(section_parts))
        
        # 子ノードを再帰処理
        children = n.get("children") or []
        if isinstance(children, list):
            for ch in children:
                if isinstance(ch, dict):
                    walk(ch, depth + 1)
    
    walk(node, 0)
    return "\n\n".join(parts)


def _get_node_by_path(root: Dict[str, Any], node_path: Sequence[int]) -> Dict[str, Any]:
    cur: Dict[str, Any] = root
    for idx in node_path:
        children = cur.get("children") or []
        if not isinstance(children, list):
            raise KeyError(f"Invalid path {list(node_path)}: children is not a list at {cur.get('section_title')!r}")
        if idx < 0 or idx >= len(children):
            raise KeyError(f"Invalid path {list(node_path)}: index out of range: {idx}")
        ch = children[idx]
        if not isinstance(ch, dict):
            raise KeyError(f"Invalid path {list(node_path)}: child is not an object at index {idx}")
        cur = ch
    return cur


def _title_path_for_node(root: Dict[str, Any], node_path: Sequence[int]) -> List[str]:
    titles: List[str] = []
    cur = root
    t0 = cur.get("section_title")
    if isinstance(t0, str) and t0.strip():
        titles.append(t0.strip())
    for idx in node_path:
        children = cur.get("children") or []
        if not isinstance(children, list) or idx < 0 or idx >= len(children):
            break
        ch = children[idx]
        if not isinstance(ch, dict):
            break
        cur = ch
        tt = cur.get("section_title")
        if isinstance(tt, str) and tt.strip():
            titles.append(tt.strip())
    return titles


def _make_chunk_id(doc_id: str, node_path: Sequence[int]) -> str:
    return f"{doc_id}:" + "/".join(str(i) for i in node_path)


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    doc_id: str
    node_path: List[int]
    title_path: List[str]
    section_title: str
    content: str

    def preview(self, *, max_chars: int = 120) -> str:
        s = _normalize_text(self.content)
        if len(s) <= max_chars:
            return s
        return s[:max_chars].rstrip() + "…"


def load_ast(ast_path: str) -> Dict[str, Any]:
    ast = _load_json(ast_path)
    root = ast.get("root")
    if not isinstance(root, dict):
        raise ValueError(f"Invalid AST: root must be an object ({ast_path})")
    return ast


ChunkStrategy = Literal["all_leaf", "level"]


def extract_chunks(
    *,
    ast: Dict[str, Any],
    doc_id: str,
    include_empty_content: bool = False,
    max_content_chars: Optional[int] = None,
    strategy: ChunkStrategy = "all_leaf",
    target_level: int = 1,
    min_chars_for_split: int = 3000,
) -> List[Chunk]:
    """
    AST からチャンクを抽出する。

    Args:
        ast: AST 辞書（root キーを持つ）
        doc_id: ドキュメント識別子
        include_empty_content: 空コンテンツのチャンクも含めるか
        max_content_chars: チャンクの最大文字数（超過時は切り詰め）
        strategy: チャンク化戦略
            - "all_leaf": 最下層ノード（リーフノード）でチャンク化（デフォルト）
            - "level": 指定レベルでチャンク化。ただし文字数が閾値以上なら1レベル下に分割
        target_level: strategy="level" のとき、チャンク化する階層レベル
                      （root=0, root直下=1, その下=2, ...）
        min_chars_for_split: strategy="level" のとき、このチャンク文字数以上なら
                             1レベル下でチャンク化する閾値

    Returns:
        Chunk のリスト
    """
    root = ast.get("root")
    if not isinstance(root, dict):
        raise ValueError("Invalid AST: root must be an object")

    if strategy == "all_leaf":
        return _extract_leaf_chunks(
            root=root,
            doc_id=doc_id,
            include_empty_content=include_empty_content,
            max_content_chars=max_content_chars,
        )
    elif strategy == "level":
        return _extract_level_chunks(
            root=root,
            doc_id=doc_id,
            include_empty_content=include_empty_content,
            max_content_chars=max_content_chars,
            target_level=target_level,
            min_chars_for_split=min_chars_for_split,
        )
    else:
        raise ValueError(f"Unknown strategy: {strategy}")


def _extract_leaf_chunks(
    *,
    root: Dict[str, Any],
    doc_id: str,
    include_empty_content: bool = False,
    max_content_chars: Optional[int] = None,
) -> List[Chunk]:
    """最下層ノード（リーフノード）でチャンク化する。"""
    out: List[Chunk] = []
    for node, path in _iter_nodes(root):
        if not _is_leaf(node):
            continue
        content = node.get("content")
        text = content if isinstance(content, str) else ""
        text = text.strip()
        if max_content_chars is not None and max_content_chars > 0 and len(text) > max_content_chars:
            text = text[:max_content_chars].rstrip() + "\n...(truncated)"
        if (not text) and (not include_empty_content):
            continue
        section_title = str(node.get("section_title") or "")
        title_path = _title_path_for_node(root, path)
        out.append(
            Chunk(
                chunk_id=_make_chunk_id(doc_id, path),
                doc_id=doc_id,
                node_path=list(path),
                title_path=title_path,
                section_title=section_title,
                content=text,
            )
        )
    return out


def _extract_level_chunks(
    *,
    root: Dict[str, Any],
    doc_id: str,
    include_empty_content: bool = False,
    max_content_chars: Optional[int] = None,
    target_level: int = 1,
    min_chars_for_split: int = 3000,
) -> List[Chunk]:
    """
    指定レベルでチャンク化する。

    ロジック:
    1. 指定されたtarget_levelでチャンク化
    2. チャンクの文字数が min_chars_for_split 以上の場合は1レベル下に分割（再帰的）
    3. ただし、1レベル下の子ノードがすべてリーフの場合は分割せず、
       content_summary を使用してチャンク化（リーフへの分割を回避）
    """
    out: List[Chunk] = []
    visited_paths: set = set()  # 重複防止用

    def _process_node(node: Dict[str, Any], path: List[int], current_level: int) -> None:
        path_key = tuple(path)
        if path_key in visited_paths:
            return
        
        # 目標レベルに達した場合、または目標レベルより深いレベル（分割された場合）
        if current_level >= target_level:
            # このノード配下のすべてのコンテンツを集約
            text = _collect_subtree_content(node)
            
            # 文字数が閾値以上で、かつリーフでない場合は分割を検討
            if len(text) >= min_chars_for_split and not _is_leaf(node):
                children = node.get("children") or []
                if isinstance(children, list):
                    # 子ノードがすべてリーフかどうかを確認
                    child_dicts = [ch for ch in children if isinstance(ch, dict)]
                    all_children_are_leaves = all(_is_leaf(ch) for ch in child_dicts)
                    
                    if all_children_are_leaves:
                        # 子ノードがすべてリーフの場合は、分割せずにcontent_summaryを使用
                        content_summary = node.get("content_summary")
                        if content_summary and isinstance(content_summary, str) and content_summary.strip():
                            text = content_summary.strip()
                        # content_summaryがない場合は、元のtextをそのまま使用（フォールバック）
                        # チャンク化処理に進む（returnしない）
                    else:
                        # 子ノードにリーフでないノードがある場合は分割
                        for i, ch in enumerate(children):
                            if isinstance(ch, dict):
                                child_path = path + [i]
                                _process_node(ch, child_path, current_level + 1)
                        return
            
            # チャンクとして追加
            visited_paths.add(path_key)
            if max_content_chars is not None and max_content_chars > 0 and len(text) > max_content_chars:
                text = text[:max_content_chars].rstrip() + "\n...(truncated)"
            if (not text) and (not include_empty_content):
                return
            section_title = str(node.get("section_title") or "")
            title_path = _title_path_for_node(root, path)
            out.append(
                Chunk(
                    chunk_id=_make_chunk_id(doc_id, path),
                    doc_id=doc_id,
                    node_path=list(path),
                    title_path=title_path,
                    section_title=section_title,
                    content=text,
                )
            )
        elif current_level < target_level:
            # 目標レベルに達していない場合は子ノードを処理
            children = node.get("children") or []
            if isinstance(children, list):
                for i, ch in enumerate(children):
                    if isinstance(ch, dict):
                        _process_node(ch, path + [i], current_level + 1)
            else:
                # 子ノードがない（リーフノード）で、まだ目標レベルに達していない場合は
                # そのリーフノードでチャンク化（目標レベルまで到達できない場合のフォールバック）
                text = _collect_subtree_content(node)
                if (not text) and (not include_empty_content):
                    return
                visited_paths.add(path_key)
                if max_content_chars is not None and max_content_chars > 0 and len(text) > max_content_chars:
                    text = text[:max_content_chars].rstrip() + "\n...(truncated)"
                section_title = str(node.get("section_title") or "")
                title_path = _title_path_for_node(root, path)
                out.append(
                    Chunk(
                        chunk_id=_make_chunk_id(doc_id, path),
                        doc_id=doc_id,
                        node_path=list(path),
                        title_path=title_path,
                        section_title=section_title,
                        content=text,
                    )
                )
        # current_level > target_level の場合は何もしない（既に目標レベルを超えている）

    _process_node(root, [], 0)
    return out


# 後方互換性のためのエイリアス
def extract_leaf_chunks(
    *,
    ast: Dict[str, Any],
    doc_id: str,
    include_empty_content: bool = False,
    max_content_chars: Optional[int] = None,
) -> List[Chunk]:
    """各ドキュメントの最下層ノードの content をチャンクとして抽出する。（後方互換性のためのエイリアス）"""
    return extract_chunks(
        ast=ast,
        doc_id=doc_id,
        include_empty_content=include_empty_content,
        max_content_chars=max_content_chars,
        strategy="all_leaf",
    )


# ---------------------------
# Hybrid search (cosine + keyword)
# ---------------------------

_WORD_RE = re.compile(r"[A-Za-z0-9_]+|[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]+")


def _extract_keywords(text: str) -> List[str]:
    """
    キーワード用トークン:
    - 英数字単語
    - 日本語連続文字列は文字bigramに分割（簡易）
    """
    s = _normalize_text(text)
    if not s:
        return []
    raw = _WORD_RE.findall(s)
    out: List[str] = []
    for tok in raw:
        t = tok.strip()
        if not t:
            continue
        # 英数字はそのまま（小文字化）
        if re.fullmatch(r"[A-Za-z0-9_]+", t):
            tl = t.lower()
            if len(tl) >= 2:
                out.append(tl)
            continue
        # 日本語っぽい連続文字は bigram
        if len(t) <= 2:
            out.append(t)
            continue
        for i in range(0, len(t) - 1):
            out.append(t[i : i + 2])
    return out


def _sha256_utf8(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


def _l2_norm(vec: Sequence[float]) -> float:
    ss = 0.0
    for x in vec:
        ss += float(x) * float(x)
    return math.sqrt(ss) if ss > 0.0 else 0.0


def _cosine_dense(a: Sequence[float], a_norm: float, b: Sequence[float], b_norm: float) -> float:
    if a_norm <= 0.0 or b_norm <= 0.0:
        return 0.0
    dot = 0.0
    # 念のため長さ差に対応（通常は同次元）
    for x, y in zip(a, b):
        dot += float(x) * float(y)
    return float(dot / (a_norm * b_norm))


def _is_azure_provider(provider: str) -> bool:
    p = (provider or "").lower()
    return p in {"azure", "azureopenai", "azure_openai"}


def _normalize_openai_embedding_model_id(model: str) -> str:
    """
    ユーザーの表記ゆれ吸収（OpenAI側の model id のみ対象）。
    Azure は deployment 名が任意なので変換しない。
    """
    m = (model or "").strip()
    ml = m.lower()
    if ml in {"embedding-large-3", "embedding-3-large", "embedding_large_3"}:
        return "text-embedding-3-large"
    if ml in {"embedding-small-3", "embedding-3-small", "embedding_small_3"}:
        return "text-embedding-3-small"
    return m


def _build_embeddings_and_id(*, model_override: Optional[str] = None) -> Tuple[Any, str]:
    """
    LangChainのEmbeddingsを構築し、キャッシュ識別子（embedding_id）も返す。

    - `LLM_PROVIDER=openai|azure|gemini` をサポート
    - OpenAI/Azure/Gemini いずれも LangChain 経由で統一（OpenAI SDK直呼びはしない）
    """
    from src.llm_provider import build_embeddings, provider_ids

    provider = (os.getenv("LLM_PROVIDER") or "openai").lower()
    mo = (model_override or "").strip()
    if mo and not _is_azure_provider(provider) and provider not in {"gemini", "google", "google_genai"}:
        # OpenAI embeddings のみ表記ゆれ吸収
        mo = _normalize_openai_embedding_model_id(mo)

    embeddings = build_embeddings(model=mo or None)
    ids = provider_ids(embedding_model=mo or None)
    return embeddings, ids.embedding_id




@dataclass(frozen=True)
class SearchResult:
    chunk: Chunk
    score: float
    cosine: float
    keyword: float
    title_keyword: float = 0.0  # title_path のキーワード類似度


class HybridChunkIndex:
    """
    インメモリの“ベクトルDB”:
    - embedding: OpenAI / Azure OpenAI の text-embedding-3-large のコサイン類似度
    - keyword: 簡易キーワード一致率
    """

    def __init__(
        self,
        *,
        chunks: List[Chunk],
        embedding_model: Optional[str] = None,
        cache_path: str = "data/embedding/embedding_cache.json",
        batch_size: int = 64,
    ) -> None:
        self.chunks = list(chunks)
        self.embedding_model = (str(embedding_model).strip() if embedding_model is not None else "")
        self.cache_path = str(cache_path or "")
        self._cache_lock_path = (self.cache_path + ".lock") if self.cache_path else ""
        self.batch_size = max(1, int(batch_size))

        # Embeddingsは provider に応じてLangChainで統一
        self._embeddings, self._embedding_id = _build_embeddings_and_id(
            model_override=(self.embedding_model or None)
        )

        self._vecs, self._norms = self._embed_chunks_with_cache()
        self._kw_sets = [set(_extract_keywords(c.content)) for c in self.chunks]
        # title_path のキーワードセット（第一階層を除外）
        self._title_kw_sets = [
            set(_extract_keywords(" ".join(c.title_path[1:]))) if len(c.title_path) > 1 else set()
            for c in self.chunks
        ]

    def _cache_lock(self):
        # cache_path が空ならロック不要
        if not self.cache_path:
            return contextlib.nullcontext()
        return _exclusive_file_lock(self._cache_lock_path)

    def _load_cache_unlocked(self) -> Dict[str, Any]:
        if not self.cache_path:
            return {"embeddings": {}}
        if not os.path.exists(self.cache_path):
            return {"embeddings": {}}
        try:
            data = _load_json(self.cache_path)
            if not isinstance(data, dict):
                return {"embeddings": {}}
            em = data.get("embeddings")
            if not isinstance(em, dict):
                data["embeddings"] = {}
            return data
        except Exception:
            return {"embeddings": {}}

    def _save_cache_unlocked(self, cache: Dict[str, Any]) -> None:
        if not self.cache_path:
            return
        if not isinstance(cache, dict):
            return
        cache.setdefault("meta", {})
        if isinstance(cache.get("meta"), dict):
            cache["meta"]["schema"] = 1
            cache["meta"]["embedding_id"] = self._embedding_id
        _atomic_write_text(self.cache_path, _dump_json(cache))

    def _cache_key_for_text(self, text: str) -> str:
        # embedding_id + normalized text hash でキャッシュ
        norm = _normalize_text(text)
        return f"{self._embedding_id}:{_sha256_utf8(norm)}"

    def _embed_chunks_with_cache(self) -> Tuple[List[List[float]], List[float]]:
        # 読み取りは短時間なのでロックし、API呼び出し中はロックしない（並列実行の詰まり回避）
        with self._cache_lock():
            cache = self._load_cache_unlocked()
            emb_map = cache.get("embeddings")
            if not isinstance(emb_map, dict):
                emb_map = {}
                cache["embeddings"] = emb_map

        vecs: List[Optional[List[float]]] = [None] * len(self.chunks)
        missing_texts: List[str] = []
        missing_indices: List[int] = []

        for i, ch in enumerate(self.chunks):
            key = self._cache_key_for_text(ch.content)
            v = emb_map.get(key)
            if isinstance(v, list) and v:
                try:
                    vec = [float(x) for x in v]
                    vecs[i] = vec
                    continue
                except Exception:
                    pass
            missing_indices.append(i)
            missing_texts.append(ch.content)

        # 埋め込み取得（バッチ）
        to_write: List[Tuple[str, List[float]]] = []
        if missing_texts:
            for start in range(0, len(missing_texts), self.batch_size):
                batch = missing_texts[start : start + self.batch_size]
                batch_vecs = self._embeddings.embed_documents(batch)
                for j, vec in enumerate(batch_vecs):
                    idx = missing_indices[start + j]
                    clean_vec = [float(x) for x in (vec or [])]
                    vecs[idx] = clean_vec
                    key = self._cache_key_for_text(self.chunks[idx].content)
                    to_write.append((key, clean_vec))

            # 保存時に最新のキャッシュを再ロードしてマージ（同時実行での取りこぼし防止）
            if self.cache_path and to_write:
                with self._cache_lock():
                    cache2 = self._load_cache_unlocked()
                    emb_map2 = cache2.get("embeddings")
                    if not isinstance(emb_map2, dict):
                        emb_map2 = {}
                        cache2["embeddings"] = emb_map2
                    for k, v in to_write:
                        emb_map2[k] = v
                    self._save_cache_unlocked(cache2)

        final_vecs: List[List[float]] = []
        norms: List[float] = []
        for v in vecs:
            vec = v or []
            final_vecs.append(vec)
            norms.append(_l2_norm(vec))
        return final_vecs, norms

    def _embed_query_with_cache(self, text: str) -> List[float]:
        q = _normalize_text(text)
        if not q:
            return []

        key = self._cache_key_for_text(q)

        # まずはキャッシュヒットを確認（短時間なのでロック）
        if self.cache_path:
            with self._cache_lock():
                cache = self._load_cache_unlocked()
                emb_map = cache.get("embeddings")
                if not isinstance(emb_map, dict):
                    emb_map = {}
                    cache["embeddings"] = emb_map
                v = emb_map.get(key)
                if isinstance(v, list) and v:
                    try:
                        return [float(x) for x in v]
                    except Exception:
                        pass

        try:
            v = self._embeddings.embed_query(q)
            vec = [float(x) for x in (v or [])]
        except Exception:
            # 一部Embeddings実装は embed_query を持たない可能性があるためフォールバック
            vecs = self._embeddings.embed_documents([q])
            vec = [float(x) for x in (vecs[0] or [])] if vecs else []

        # 保存は「再ロード→再チェック→書き込み」で競合と取りこぼしを回避
        if self.cache_path:
            with self._cache_lock():
                cache2 = self._load_cache_unlocked()
                emb_map2 = cache2.get("embeddings")
                if not isinstance(emb_map2, dict):
                    emb_map2 = {}
                    cache2["embeddings"] = emb_map2
                v2 = emb_map2.get(key)
                if isinstance(v2, list) and v2:
                    try:
                        return [float(x) for x in v2]
                    except Exception:
                        pass
                emb_map2[key] = vec
                self._save_cache_unlocked(cache2)
        return vec

    def search(
        self,
        *,
        query: str,
        query_title_path: Optional[List[str]] = None,
        top_k: int = 5,
        alpha: float = 0.5,
        beta: float = 0.3,
        min_score: Optional[float] = None,
    ) -> List[SearchResult]:
        """
        ハイブリッド検索

        Args:
            query: 検索クエリ（チャンクのコンテンツ）
            query_title_path: クエリのtitle_path（第一階層は除外して比較）
            top_k: 返す結果数
            alpha: コサイン類似度の重み
            beta: title_path類似度の重み
            min_score: 最小スコア閾値

        統合スコア = alpha * cosine + beta * title_keyword + (1 - alpha - beta) * content_keyword
        """
        q = _normalize_text(query)
        if not q:
            return []
        q_vec = self._embed_query_with_cache(q)
        q_norm = _l2_norm(q_vec)
        q_kw = set(_extract_keywords(q))

        # query_title_path のキーワード（第一階層を除外）
        if query_title_path and len(query_title_path) > 1:
            q_title_kw = set(_extract_keywords(" ".join(query_title_path[1:])))
        else:
            q_title_kw = set()

        # 重みの正規化（合計が1を超えないように）
        alpha_f = max(0.0, min(1.0, float(alpha)))
        beta_f = max(0.0, min(1.0 - alpha_f, float(beta)))
        gamma_f = 1.0 - alpha_f - beta_f  # content_keyword の重み

        scored: List[SearchResult] = []
        for i, ch in enumerate(self.chunks):
            cosine = _cosine_dense(q_vec, q_norm, self._vecs[i], self._norms[i])

            # コンテンツのキーワード類似度
            if q_kw:
                inter = len(q_kw & self._kw_sets[i])
                keyword = inter / max(1, len(q_kw))
            else:
                keyword = 0.0

            # title_path のキーワード類似度
            if q_title_kw:
                title_inter = len(q_title_kw & self._title_kw_sets[i])
                title_keyword = title_inter / max(1, len(q_title_kw))
            else:
                title_keyword = 0.0

            score = alpha_f * cosine + beta_f * title_keyword + gamma_f * keyword
            if min_score is not None and score < float(min_score):
                continue
            scored.append(SearchResult(
                chunk=ch, score=score, cosine=cosine,
                keyword=keyword, title_keyword=title_keyword
            ))

        scored.sort(key=lambda r: r.score, reverse=True)
        return scored[: max(1, int(top_k))]


# ---------------------------
# All-chunk matching / chunk compare
# ---------------------------


def match_all_chunks(
    *,
    chunks_a: List[Chunk],
    index_b: HybridChunkIndex,
    top_k: int = 3,
    alpha: float = 0.5,
    beta: float = 0.3,
    min_score: float = 0.25,
) -> Dict[str, Any]:
    """
    全チャンク類似度マッチング:
    docA の各チャンクをクエリにして docB から top_k を返す。

    Args:
        alpha: コサイン類似度の重み
        beta: title_path類似度の重み
        (1 - alpha - beta): コンテンツキーワード類似度の重み
    """
    groups: List[Dict[str, Any]] = []
    used_b: set[str] = set()

    for ch_a in chunks_a:
        hits = index_b.search(
            query=ch_a.content,
            query_title_path=ch_a.title_path,
            top_k=top_k,
            alpha=alpha,
            beta=beta,
            min_score=min_score,
        )
        matches = []
        for h in hits:
            used_b.add(h.chunk.chunk_id)
            matches.append(
                {
                    "chunk_id": h.chunk.chunk_id,
                    "score": h.score,
                    "cosine": h.cosine,
                    "keyword": h.keyword,
                    "title_keyword": h.title_keyword,
                    "title_path": h.chunk.title_path,
                    "preview": h.chunk.preview(),
                }
            )
        groups.append(
            {
                "chunk_id": ch_a.chunk_id,
                "title_path": ch_a.title_path,
                "preview": ch_a.preview(),
                "matches": matches,
            }
        )

    all_b = {c.chunk_id for c in index_b.chunks}
    unmatched_b = sorted(all_b - used_b)
    return {"ok": True, "groups": groups, "unmatched_b": unmatched_b}


def _get_node_chain(ast: Dict[str, Any], node_path: Sequence[int]) -> List[Dict[str, Any]]:
    """ASTルートからnode_pathで指定されたノードまでのチェーンを取得する。"""
    root = ast.get("root")
    if not isinstance(root, dict):
        raise ValueError("Invalid AST: root must be an object")

    chain: List[Dict[str, Any]] = [root]
    cur = root
    for idx in node_path:
        children = cur.get("children") or []
        if not isinstance(children, list) or idx < 0 or idx >= len(children):
            break
        ch = children[idx]
        if not isinstance(ch, dict):
            break
        chain.append(ch)
        cur = ch
    return chain


def _get_ancestor_context(chain: List[Dict[str, Any]], fallback_to_content: bool = True) -> List[str]:
    """祖先ノード（leafを除く）からcontent_summaryのリストを取得する。"""
    contexts: List[str] = []
    # ancestors only (exclude leaf itself)
    for n in chain[:-1]:
        summ = n.get("content_summary")
        ss = summ.strip() if isinstance(summ, str) else ""
        if not ss and fallback_to_content:
            c = n.get("content")
            ss = c.strip() if isinstance(c, str) else ""
        if ss:
            contexts.append(ss)
    return contexts


def build_merged_compare_text_for_diff(
    *,
    ast: Dict[str, Any],
    chunks: List["Chunk"],
    max_chars_per_content: int = 0,
) -> str:
    """
    【diff用】複数チャンクをマージして比較用テキストを作る。
    
    - content_summaryは使用しない（LLM生成のため必ず差分になる）
    - 上位チャンク（非leaf）の場合は配下のcontentを全て結合
    - 階層パスは含めない
    - セクションタイトルは含める
    - 複数チャンク指定時は重複を統合
    
    Args:
        ast: ASTデータ
        chunks: チャンクリスト
        max_chars_per_content: 各contentの最大文字数（0以下は無制限）
    """
    if not chunks:
        return ""

    root = ast.get("root")
    if not isinstance(root, dict):
        return ""

    # node_pathで重複を除去し、親子関係を整理
    # 親が含まれている場合は子を除外（親の配下に含まれるため）
    unique_paths: List[Tuple[int, ...]] = []
    sorted_chunks = sorted(chunks, key=lambda c: len(c.node_path))  # 短いパス（上位階層）を先に
    
    for ch in sorted_chunks:
        path_tuple = tuple(ch.node_path)
        # 既存のパスの子孫かどうかをチェック
        is_descendant = False
        for existing_path in unique_paths:
            if len(path_tuple) >= len(existing_path) and path_tuple[:len(existing_path)] == existing_path:
                is_descendant = True
                break
        if not is_descendant:
            unique_paths.append(path_tuple)

    # 重複除去後のチャンクを取得
    unique_chunks = [ch for ch in chunks if tuple(ch.node_path) in unique_paths]

    # 各チャンクのテキストを生成
    all_parts: List[str] = []
    for ch in unique_chunks:
        try:
            node = _get_node_by_path(root, ch.node_path)
        except KeyError:
            continue
        
        # 配下のcontentをセクションタイトル付きで収集
        text = _collect_subtree_content_with_titles(
            node,
            max_chars_per_content=max_chars_per_content,
            chunk_id_for_reference=ch.chunk_id,
        )
        if text:
            all_parts.append(text)

    return "\n\n".join(all_parts)


def build_merged_compare_text(
    *,
    ast: Dict[str, Any],
    chunks: List["Chunk"],
    fallback_to_ancestor_content: bool = True,
    max_chars_per_content: int = 0,
) -> str:
    """
    【LLM用】複数チャンクを階層構造を保持しながらマージして比較用テキストを作る。
    
    - 上位階層のcontent_summaryをコンテキストとして含める
    - 上位チャンク（非leaf）の場合は配下のcontent_summary+contentを全て含める
    - 同一の上位階層を持つチャンクはグループ化
    
    Args:
        ast: ASTデータ
        chunks: チャンクリスト
        fallback_to_ancestor_content: content_summaryがない場合にcontentを使うか
        max_chars_per_content: 各contentの最大文字数（0以下は無制限）
    """
    if not chunks:
        return ""

    root = ast.get("root")
    if not isinstance(root, dict):
        return ""

    # node_pathで重複を除去し、親子関係を整理
    # 親が含まれている場合は子を除外（親の配下に含まれるため）
    unique_paths: List[Tuple[int, ...]] = []
    sorted_chunks = sorted(chunks, key=lambda c: len(c.node_path))  # 短いパス（上位階層）を先に
    
    for ch in sorted_chunks:
        path_tuple = tuple(ch.node_path)
        # 既存のパスの子孫かどうかをチェック
        is_descendant = False
        for existing_path in unique_paths:
            if len(path_tuple) >= len(existing_path) and path_tuple[:len(existing_path)] == existing_path:
                is_descendant = True
                break
        if not is_descendant:
            unique_paths.append(path_tuple)

    # 重複除去後のチャンクを取得
    unique_chunks = [ch for ch in chunks if tuple(ch.node_path) in unique_paths]

    # 各チャンクのnode_chainと祖先コンテキストを取得
    chunk_data: List[Dict[str, Any]] = []
    for ch in unique_chunks:
        chain = _get_node_chain(ast, ch.node_path)
        ancestor_contexts = _get_ancestor_context(chain, fallback_to_ancestor_content)
        
        # 対象ノードを取得
        target_node = chain[-1] if chain else None
        
        # 非leafの場合は配下のcontent_summary+contentを全て含める
        if target_node and not _is_leaf(target_node):
            # 配下の内容をセクションタイトル・content_summary・content付きで収集
            subtree_content = _collect_subtree_content_with_summaries(
                target_node,
                max_chars_per_content=max_chars_per_content,
                chunk_id_for_reference=ch.chunk_id,
            )
            leaf_content = subtree_content
        else:
            # leafの場合は従来通り
            leaf_content = target_node.get("content") if target_node else ""
            leaf_content = leaf_content.strip() if isinstance(leaf_content, str) else ""
            # 文字数制限
            if max_chars_per_content > 0 and len(leaf_content) > max_chars_per_content:
                leaf_content = leaf_content[:max_chars_per_content].rstrip()
                leaf_content += f"\n...(truncated) ※全文を参照する場合はチャンク {ch.chunk_id} を直接参照してください"
        
        chunk_data.append({
            "chunk": ch,
            "chain": chain,
            "ancestor_contexts": ancestor_contexts,
            "leaf_content": leaf_content,
            "is_leaf": target_node is None or _is_leaf(target_node),
        })

    # 共通の祖先コンテキストを特定
    if len(chunk_data) == 1:
        # 単一チャンクの場合は従来と同様の形式
        cd = chunk_data[0]
        parts: List[str] = []
        parts.append(f"[チャンクID: {cd['chunk'].chunk_id}]")
        parts.append(f"[階層パス: {' > '.join(cd['chunk'].title_path)}]")
        for ctx in cd["ancestor_contexts"]:
            parts.append(f"[文章のコンテキスト]: {ctx}")
        if cd["leaf_content"]:
            if cd["is_leaf"]:
                parts.append(f"[比較対象の文章]: {cd['leaf_content']}")
            else:
                # 非leafの場合は配下の内容をそのまま追加（既にフォーマット済み）
                parts.append("[配下のセクション内容]:")
                parts.append(cd["leaf_content"])
        return "\n\n".join(parts)

    # 複数チャンクの場合: 共通祖先を見つけてグループ化
    # まず全チャンクの共通祖先コンテキストを特定
    all_ancestor_contexts = [tuple(cd["ancestor_contexts"]) for cd in chunk_data]
    
    # 共通プレフィックスを計算
    common_prefix_len = 0
    if all_ancestor_contexts:
        min_len = min(len(ctx) for ctx in all_ancestor_contexts)
        for i in range(min_len):
            if all(ctx[i] == all_ancestor_contexts[0][i] for ctx in all_ancestor_contexts):
                common_prefix_len = i + 1
            else:
                break

    parts: List[str] = []

    # 共通の上位コンテキストを出力
    if common_prefix_len > 0:
        parts.append("=== 共通の上位階層コンテキスト ===")
        common_contexts = all_ancestor_contexts[0][:common_prefix_len]
        for i, ctx in enumerate(common_contexts):
            indent = "  " * i
            parts.append(f"{indent}[階層{i+1}のコンテキスト]: {ctx}")
        parts.append("")

    # 各チャンクを階層構造を示しながら出力
    parts.append("=== 各チャンクの内容 ===")
    for idx, cd in enumerate(chunk_data, 1):
        chunk = cd["chunk"]
        parts.append(f"\n--- チャンク{idx} ---")
        parts.append(f"[チャンクID: {chunk.chunk_id}]")
        parts.append(f"[階層パス: {' > '.join(chunk.title_path)}]")
        
        # 共通でない祖先コンテキストのみ表示
        remaining_contexts = cd["ancestor_contexts"][common_prefix_len:]
        if remaining_contexts:
            for i, ctx in enumerate(remaining_contexts):
                depth = common_prefix_len + i + 1
                parts.append(f"[階層{depth}のコンテキスト]: {ctx}")
        
        if cd["leaf_content"]:
            if cd["is_leaf"]:
                parts.append(f"[比較対象の文章]: {cd['leaf_content']}")
            else:
                # 非leafの場合は配下の内容をそのまま追加
                parts.append("[配下のセクション内容]:")
                parts.append(cd["leaf_content"])

    return "\n".join(parts)


def build_compare_text(
    *,
    ast: Dict[str, Any],
    node_path: Sequence[int],
    include_titles: bool = True,
    include_ancestor_summaries: bool = True,
    fallback_to_ancestor_content: bool = True,
) -> str:
    """leaf content + 上位階層content_summary を結合して比較用テキストを作る。"""
    root = ast.get("root")
    if not isinstance(root, dict):
        raise ValueError("Invalid AST: root must be an object")

    # chain nodes from root to target
    chain: List[Dict[str, Any]] = [root]
    cur = root
    for idx in node_path:
        children = cur.get("children") or []
        if not isinstance(children, list) or idx < 0 or idx >= len(children):
            break
        ch = children[idx]
        if not isinstance(ch, dict):
            break
        chain.append(ch)
        cur = ch

    target = chain[-1]

    parts: List[str] = []

    if include_titles:
        title_path = _title_path_for_node(root, node_path)
        if title_path:
            parts.append(" > ".join(title_path))

    if include_ancestor_summaries and len(chain) >= 2:
        # ancestors only (exclude leaf itself)
        for n in chain[:-1]:
            summ = n.get("content_summary")
            ss = summ.strip() if isinstance(summ, str) else ""
            if not ss and fallback_to_ancestor_content:
                c = n.get("content")
                ss = c.strip() if isinstance(c, str) else ""
            if ss:
                # 背景情報であることがわかるようにタグ付け
                parts.append(f"[文章のコンテキスト]: {ss}")

    # leaf content
    leaf_content = target.get("content")
    lc = leaf_content.strip() if isinstance(leaf_content, str) else ""
    if lc:
        parts.append(f"[比較対象の文章]: {lc}")

    return "\n\n".join([p for p in parts if p.strip()]).strip()


def _build_chat_llm_and_id(*, model_override: Optional[str] = None) -> Tuple[Any, str]:
    """
    LangChainのChatモデルを構築し、識別子（chat_id）も返す。
    """
    from src.llm_provider import build_chat_llm, provider_ids

    mo = (model_override or "").strip()
    llm = build_chat_llm(model=mo or None)
    ids = provider_ids(chat_model=mo or None)
    return llm, ids.chat_id


def _truncate_for_llm(text: str, *, max_chars: int) -> str:
    if max_chars is None or max_chars <= 0:
        return text
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n...(truncated)"


def _extract_first_json_object(s: str) -> Optional[Dict[str, Any]]:
    if not isinstance(s, str) or not s.strip():
        return None
    t = s.strip()
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass

    # 粗い復旧: 最初の { 〜 最後の } を抜き出して再パース
    start = t.find("{")
    end = t.rfind("}")
    if start >= 0 and end > start:
        cand = t[start : end + 1]
        try:
            obj = json.loads(cand)
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
    return None


def _llm_extract_differences(
    *,
    text_a: str,
    text_b: str,
    model_override: Optional[str] = None,
    max_input_chars_per_text: int = 12000,
) -> Dict[str, Any]:
    """
    LLMで2テキストの差分を抽出して JSON（dict）として返す。
    """
    llm, chat_id = _build_chat_llm_and_id(model_override=model_override)

    a = _truncate_for_llm(text_a, max_chars=max_input_chars_per_text)
    b = _truncate_for_llm(text_b, max_chars=max_input_chars_per_text)

    system = (
        "あなたはドキュメントの差分抽出アシスタントです。"
        "与えられたテキストAとテキストBを比較し、差分を抽出してください。"
        "日本語で、根拠（A/Bのどの記述に基づくか）が分かるように短い引用も添えてください。"
        "出力は必ず JSON のみ（前後に余計な説明文を付けない）で返してください。"
    )

    schema_hint = {
        "summary": "全体の差分要約（日本語・1〜3文）",
        "changes": [
            {
                "type": "A_only|B_only|both_but_different",
                "topic": "差分の対象（例: 閾値/例外条件/必須項目など）",
                "impact": "影響/意味（日本語・簡潔）",
                "A_content": "A側の内容（該当しない場合は空文字）",
                "B_content": "B側の内容（該当しない場合は空文字）",
                "A_evidence": "Aからの短い引用（無ければ空文字）",
                "B_evidence": "Bからの短い引用（無ければ空文字）",
            }
        ],
    }

    user = (
        "次の2つのテキストを比較し、差分を抽出してください。\n\n"
        f"【テキストA】\n{a}\n\n"
        f"【テキストB】\n{b}\n\n"
        "出力は次のJSONスキーマに従ってください（キー名は固定、値はあなたが埋める）。\n"
        + _dump_json(schema_hint)
    )

    from langchain_core.messages import HumanMessage, SystemMessage

    resp = llm.invoke([SystemMessage(content=system), HumanMessage(content=user)])
    content = getattr(resp, "content", "") or ""
    if not isinstance(content, str):
        try:
            content = json.dumps(content, ensure_ascii=False, indent=2)
        except Exception:
            content = str(content)

    parsed = _extract_first_json_object(content)
    if parsed is None:
        return {"ok": False, "error": "LLM出力がJSONとして解釈できませんでした。", "chat_id": chat_id, "raw": content}

    return {"ok": True, "chat_id": chat_id, "diff": parsed}


def _llm_extract_differences_batch(
    *,
    text_a: str,
    text_b: str,
    chunk_ids_a: List[str],
    chunk_ids_b: List[str],
    model_override: Optional[str] = None,
    max_input_chars_per_text: int = 12000,
) -> Dict[str, Any]:
    """
    LLMで複数チャンクをまとめた2テキストの差分を抽出して JSON（dict）として返す。
    チャンクIDを含めた出力を要求し、どのチャンク間の差分かを明確にする。
    """
    llm, chat_id = _build_chat_llm_and_id(model_override=model_override)

    a = _truncate_for_llm(text_a, max_chars=max_input_chars_per_text)
    b = _truncate_for_llm(text_b, max_chars=max_input_chars_per_text)

    # チャンクID情報をプロンプトに含める
    chunk_info_a = ", ".join(chunk_ids_a) if chunk_ids_a else "（単一チャンク）"
    chunk_info_b = ", ".join(chunk_ids_b) if chunk_ids_b else "（単一チャンク）"

    system = (
        "あなたはドキュメントの差分抽出アシスタントです。\n"
        "与えられたドキュメントA（複数チャンクを含む場合あり）とドキュメントB（複数チャンクを含む場合あり）を比較し、差分を抽出してください。\n"
        "各チャンクには「チャンクID」が付与されています。差分を報告する際は、どのチャンクに関する差分かを明記してください。\n"
        "各チャンクには[文章のコンテキスト]が付与されていますがこれは該当チャンクの上位階層の要約なので比較に使用する必要はありません。比較作業は[比較対象の文章]を対象に行ってください。\n"
        "日本語で、根拠（A/Bのどの記述に基づくか）が分かるように短い引用も添えてください。\n"
        "出力は必ず JSON のみ（前後に余計な説明文を付けない）で返してください。"
    )

    schema_hint = {
        "summary": "全体の差分要約（日本語・1〜3文）",
        "changes": [
            {
                "type": "A_only|B_only|both_but_different",
                "topic": "差分の対象（例: 閾値/例外条件/必須項目など）",
                "chunk_ids_a": ["関連するAのチャンクID（複数可、該当しない場合は空配列）"],
                "chunk_ids_b": ["関連するBのチャンクID（複数可、該当しない場合は空配列）"],
                "impact": "影響/意味（日本語・簡潔）",
                "A_content": "A側の内容（該当しない場合は空文字）",
                "B_content": "B側の内容（該当しない場合は空文字）",
                "A_evidence": "Aからの短い引用（無ければ空文字）",
                "B_evidence": "Bからの短い引用（無ければ空文字）",
            }
        ],
    }

    user = (
        "次の2つのドキュメントを比較し、差分を抽出してください。\n"
        "各ドキュメントは複数のチャンク（セクション）を含む場合があります。\n"
        "階層構造が示されている場合は、その構造を考慮して比較してください。\n\n"
        f"【ドキュメントA】（チャンクID: {chunk_info_a}）\n{a}\n\n"
        f"【ドキュメントB】（チャンクID: {chunk_info_b}）\n{b}\n\n"
        "出力は次のJSONスキーマに従ってください（キー名は固定、値はあなたが埋める）。\n"
        + _dump_json(schema_hint)
    )

    from langchain_core.messages import HumanMessage, SystemMessage

    resp = llm.invoke([SystemMessage(content=system), HumanMessage(content=user)])
    content = getattr(resp, "content", "") or ""
    if not isinstance(content, str):
        try:
            content = json.dumps(content, ensure_ascii=False, indent=2)
        except Exception:
            content = str(content)

    parsed = _extract_first_json_object(content)
    if parsed is None:
        return {"ok": False, "error": "LLM出力がJSONとして解釈できませんでした。", "chat_id": chat_id, "raw": content}

    return {"ok": True, "chat_id": chat_id, "diff": parsed}


def compare_chunks(
    *,
    ast_a: Dict[str, Any],
    ast_b: Dict[str, Any],
    chunks_a_by_id: Dict[str, Chunk],
    chunks_b_by_id: Dict[str, Chunk],
    chunk_ids_a: Sequence[str],
    chunk_ids_b: Sequence[str],
    llm_model: Optional[str] = None,
    max_input_chars_per_text: int = 12000,
    max_chars_per_content: int = 3000,
) -> Dict[str, Any]:
    """
    指定チャンクの比較（1:1, 1:N, N:1, N:N）。
    複数チャンクを階層構造を保持しながらまとめて1回のLLM呼び出しで比較する。

    差分抽出は LLM で実施する（非LLMの difflib 差分は使用しない）。
    
    Args:
        ast_a: docAのASTデータ
        ast_b: docBのASTデータ
        chunks_a_by_id: docAのchunk_id→Chunkマップ
        chunks_b_by_id: docBのchunk_id→Chunkマップ
        chunk_ids_a: 比較するdocAのchunk_idリスト
        chunk_ids_b: 比較するdocBのchunk_idリスト
        llm_model: LLMモデル名（オプション）
        max_input_chars_per_text: LLMに渡すテキストの最大文字数
        max_chars_per_content: 各contentの最大文字数（超過分は切り詰め、参照メッセージ付与）
    """
    a_ids = list(chunk_ids_a)
    b_ids = list(chunk_ids_b)
    if not a_ids or not b_ids:
        return {"ok": False, "error": "chunk_ids_a と chunk_ids_b はそれぞれ1件以上指定してください。"}

    # チャンクを収集
    chunks_a: List[Chunk] = []
    missing_a: List[str] = []
    for a_id in a_ids:
        ch = chunks_a_by_id.get(a_id)
        if ch is None:
            missing_a.append(a_id)
        else:
            chunks_a.append(ch)

    chunks_b: List[Chunk] = []
    missing_b: List[str] = []
    for b_id in b_ids:
        ch = chunks_b_by_id.get(b_id)
        if ch is None:
            missing_b.append(b_id)
        else:
            chunks_b.append(ch)

    # エラーがあれば報告
    if missing_a or missing_b:
        errors = []
        if missing_a:
            errors.append(f"docA chunk_id が見つかりません: {', '.join(missing_a)}")
        if missing_b:
            errors.append(f"docB chunk_id が見つかりません: {', '.join(missing_b)}")
        return {"ok": False, "error": "; ".join(errors)}

    if not chunks_a or not chunks_b:
        return {"ok": False, "error": "有効なチャンクがありません。"}

    # 複数チャンクを階層構造を保持しながらマージ（LLM用：content_summary含む）
    text_a = build_merged_compare_text(ast=ast_a, chunks=chunks_a, max_chars_per_content=max_chars_per_content)
    text_b = build_merged_compare_text(ast=ast_b, chunks=chunks_b, max_chars_per_content=max_chars_per_content)

    # print("--------------------------------")
    # print("### debug: merged text_a ###")
    # print(text_a)
    # print("--------------------------------")
    # print("### debug: merged text_b ###")
    # print(text_b)
    # print("--------------------------------")

    # 1回のLLM呼び出しで比較
    llm_result = _llm_extract_differences_batch(
        text_a=text_a,
        text_b=text_b,
        chunk_ids_a=[ch.chunk_id for ch in chunks_a],
        chunk_ids_b=[ch.chunk_id for ch in chunks_b],
        model_override=llm_model,
        max_input_chars_per_text=max_input_chars_per_text,
    )

    # モードを判定
    mode = "N:N"
    if len(a_ids) == 1 and len(b_ids) == 1:
        mode = "1:1"
    elif len(a_ids) == 1 and len(b_ids) > 1:
        mode = "1:N"
    elif len(a_ids) > 1 and len(b_ids) == 1:
        mode = "N:1"

    return {
        "ok": bool(llm_result.get("ok")),
        "mode": mode,
        "chunk_ids_a": a_ids,
        "chunk_ids_b": b_ids,
        "title_paths_a": [ch.title_path for ch in chunks_a],
        "title_paths_b": [ch.title_path for ch in chunks_b],
        "llm": llm_result,
    }


# ---------------------------
# CLI (optional)
# ---------------------------


def _default_doc_id_from_path(p: str) -> str:
    return os.path.basename(p)


def main() -> int:
    p = argparse.ArgumentParser(description="AST chunk matcher / comparer (hybrid cosine+keyword).")
    p.add_argument("--a", required=True, help="Path to docA *.ast.json")
    p.add_argument("--b", required=True, help="Path to docB *.ast.json")
    p.add_argument("--top-k", type=int, default=3, help="Top-k matches per chunk")
    p.add_argument("--alpha", type=float, default=0.7, help="Hybrid weight: alpha*cosine + (1-alpha)*keyword")
    p.add_argument("--min-score", type=float, default=0.25, help="Min hybrid score to keep as a match")
    p.add_argument("--out", required=False, help="Write matching result JSON to this file")
    args = p.parse_args()

    ast_a = load_ast(args.a)
    ast_b = load_ast(args.b)
    doc_id_a = _default_doc_id_from_path(args.a)
    doc_id_b = _default_doc_id_from_path(args.b)
    chunks_a = extract_leaf_chunks(ast=ast_a, doc_id=doc_id_a)
    chunks_b = extract_leaf_chunks(ast=ast_b, doc_id=doc_id_b)
    index_b = HybridChunkIndex(chunks=chunks_b)
    matched = match_all_chunks(
        chunks_a=chunks_a, index_b=index_b, top_k=int(args.top_k), alpha=float(args.alpha), min_score=float(args.min_score)
    )
    text = _dump_json(matched)
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        print(f"Wrote: {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


