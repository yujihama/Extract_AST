from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from document_process_app.core.pipeline import CancelledError, RunContext


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _have_llm_key() -> bool:
    return bool(os.getenv("OPENAI_API_KEY") or os.getenv("AZURE_OPENAI_API_KEY"))


def _load_run_config(ctx: RunContext) -> dict[str, Any]:
    run_dir = Path(ctx.paths["run_dir"])
    config_path = run_dir / "config.json"
    if not config_path.exists():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_run_config(ctx: RunContext, config: dict[str, Any]) -> None:
    run_dir = Path(ctx.paths["run_dir"])
    config_path = run_dir / "config.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def _normalize_doc_id(raw: Any, fallback: str) -> str:
    text = str(raw).strip() if raw is not None else ""
    if not text:
        return fallback
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", text)
    return safe or fallback


def _get_documents(ctx: RunContext) -> list[dict[str, Any]]:
    config = _load_run_config(ctx)
    docs = config.get("documents")
    if isinstance(docs, list) and docs:
        out: list[dict[str, Any]] = []
        for i, d in enumerate(docs, 1):
            if not isinstance(d, dict):
                continue
            doc_id = _normalize_doc_id(d.get("doc_id"), f"d{i}")
            out.append({**d, "doc_id": doc_id})
        return out
    return []


def _get_doc_hash_from_docs(docs: list[dict[str, Any]], doc_id: str) -> Optional[str]:
    for d in docs:
        if str(d.get("doc_id")) == doc_id:
            h = d.get("doc_hash")
            return str(h) if h else None
    return None


def _resolve_doc_path(
    doc_id: str,
    key: str,
    docs_by_id: dict[str, dict[str, Any]],
    run_dir: Path,
    work_dir: Path,
) -> Optional[Path]:
    doc = docs_by_id.get(doc_id)
    if not doc:
        return None
    paths = doc.get("paths") or {}
    raw = paths.get(key)
    if raw:
        p = Path(str(raw))
        return p if p.is_absolute() else (run_dir / p)
    fallback_map = {
        "txt": f"doc_{doc_id}.txt",
        "ast": f"ast_{doc_id}.ast.json",
        "blueprint": f"blueprint_{doc_id}.json",
    }
    fallback = fallback_map.get(key)
    return work_dir / fallback if fallback else None


def _build_pair_compare_setup_tool(
    ctx: RunContext,
    docs_by_id: dict[str, dict[str, Any]],
    run_dir: Path,
    work_dir: Path,
):
    from langchain_core.tools import tool

    @tool
    def pair_compare_setup(doc_a_id: str, doc_b_id: str, purpose: str = "") -> str:
        """指定ペアの比較準備（compare_setup＋初期マッチング保存）"""
        a_id = str(doc_a_id or "").strip()
        b_id = str(doc_b_id or "").strip()
        if not a_id or not b_id or a_id == b_id:
            raise ValueError("doc_a_id and doc_b_id must be different and non-empty")
        if a_id not in docs_by_id or b_id not in docs_by_id:
            raise ValueError("unknown doc_id for pair_compare_setup")

        if not _have_llm_key():
            raise RuntimeError("Embedding/LLM API key not set (OPENAI_API_KEY or AZURE_OPENAI_API_KEY)")

        safe_a = _normalize_doc_id(a_id, a_id)
        safe_b = _normalize_doc_id(b_id, b_id)

        pairs_dir = work_dir / "pairs" / f"{safe_a}_{safe_b}"
        pairs_dir.mkdir(parents=True, exist_ok=True)

        # NOTE:
        # Windows + tool並列実行で work_dir/doc_<doc_id>.txt 等の上書きが WinError 32 を起こしやすい。
        # ここでは shared な一時ファイルは作らず、元の doc_{id} / ast_{id} を直接 compare_setup に渡す。
        # キャッシュもペアごとに分離してファイルロック競合を避ける。

        doc_a_txt = _resolve_doc_path(a_id, "txt", docs_by_id, run_dir, work_dir)
        doc_b_txt = _resolve_doc_path(b_id, "txt", docs_by_id, run_dir, work_dir)
        ast_a_path = _resolve_doc_path(a_id, "ast", docs_by_id, run_dir, work_dir)
        ast_b_path = _resolve_doc_path(b_id, "ast", docs_by_id, run_dir, work_dir)
        if not doc_a_txt or not doc_a_txt.exists():
            raise FileNotFoundError(str(doc_a_txt) if doc_a_txt else f"doc txt not found: {a_id}")
        if not doc_b_txt or not doc_b_txt.exists():
            raise FileNotFoundError(str(doc_b_txt) if doc_b_txt else f"doc txt not found: {b_id}")
        if not ast_a_path or not ast_a_path.exists():
            raise FileNotFoundError(str(ast_a_path) if ast_a_path else f"ast not found: {a_id}")
        if not ast_b_path or not ast_b_path.exists():
            raise FileNotFoundError(str(ast_b_path) if ast_b_path else f"ast not found: {b_id}")

        cache_path = pairs_dir / "embedding_cache.json"

        from src.tools import COMPARE_STATE, compare_all_chunk_similarity_matching, compare_setup

        try:
            COMPARE_STATE.clear()
        except Exception:
            pass

        setup_json = compare_setup.invoke(
            {
                "docA": str(ast_a_path),
                "docB": str(ast_b_path),
                "docA_txt": str(doc_a_txt),
                "docB_txt": str(doc_b_txt),
                "embedding_model": ctx.params.get("embedding_model"),
                "cache_path": str(cache_path),
                "batch_size": int(ctx.params.get("embedding_batch_size", 64)),
            }
        )

        initial_matching_json = compare_all_chunk_similarity_matching.invoke(
            {
                "top_k": int(ctx.params.get("match_top_k", 3)),
                "alpha": float(ctx.params.get("match_alpha", 0.3)),
                "beta": float(ctx.params.get("match_beta", 0.4)),
                "min_score": float(ctx.params.get("match_min_score", 0.25)),
            }
        )

        initial_matching_path = pairs_dir / "initial_matching.json"
        initial_matching_path.write_text(str(initial_matching_json), encoding="utf-8")

        try:
            ctx.events.emit(
                ctx.run_id,
                "pair_setup_done",
                {
                    "ts": _utcnow_iso(),
                    "a": a_id,
                    "b": b_id,
                    "purpose": str(purpose or ""),
                    "pair_dir": pairs_dir.relative_to(run_dir).as_posix(),
                },
            )
        except Exception:
            pass

        try:
            ctx.events.emit(
                ctx.run_id,
                "artifact_updated",
                {"ts": _utcnow_iso(), "kind": "pair_initial_matching", "path": initial_matching_path.relative_to(run_dir).as_posix()},
            )
        except Exception:
            pass

        if cache_path.exists():
            try:
                ctx.events.emit(
                    ctx.run_id,
                    "artifact_updated",
                    {"ts": _utcnow_iso(), "kind": "embedding_cache", "path": cache_path.relative_to(run_dir).as_posix()},
                )
            except Exception:
                pass

        # ドキュメントペア成果物として永続化（次runで自動再利用できるようにする）
        doc_a_hash = docs_by_id.get(a_id, {}).get("doc_hash")
        doc_b_hash = docs_by_id.get(b_id, {}).get("doc_hash")
        if doc_a_hash and doc_b_hash:
            try:
                from document_process_app.infra.document_store import DocumentPairRepository

                pair_repo = DocumentPairRepository()

                matching_data = None
                try:
                    matching_data = json.loads(initial_matching_json)
                except Exception:
                    if isinstance(COMPARE_STATE.get("initial_matching"), dict):
                        matching_data = COMPARE_STATE.get("initial_matching")
                if isinstance(matching_data, dict):
                    pair_repo.save_matching(str(doc_a_hash), str(doc_b_hash), matching_data)

                if cache_path.exists():
                    try:
                        cache_data = json.loads(cache_path.read_text(encoding="utf-8"))
                        if isinstance(cache_data, dict):
                            pair_repo.save_embedding_cache(str(doc_a_hash), str(doc_b_hash), cache_data)
                    except Exception:
                        pass
            except Exception:
                # pair_compare_setup 自体は成功しているので、永続化失敗は致命にしない
                pass

        return json.dumps(
            {
                "ok": True,
                "a": a_id,
                "b": b_id,
                "purpose": str(purpose or ""),
                "pair_dir": pairs_dir.relative_to(run_dir).as_posix(),
                "initial_matching_path": initial_matching_path.relative_to(run_dir).as_posix(),
                "cache_path": cache_path.relative_to(run_dir).as_posix() if cache_path.exists() else "",
                "setup_json_preview": str(setup_json)[:300],
            },
            ensure_ascii=False,
        )

    return pair_compare_setup


def _safe_pair_key(a_id: str, b_id: str) -> str:
    safe_a = _normalize_doc_id(a_id, a_id)
    safe_b = _normalize_doc_id(b_id, b_id)
    return f"{safe_a}_{safe_b}"


def _ensure_compare_state_for_pair(
    ctx: RunContext,
    *,
    docs_by_id: dict[str, dict[str, Any]],
    run_dir: Path,
    work_dir: Path,
    doc_a_id: str,
    doc_b_id: str,
) -> tuple[str, Path]:
    """
    指定ペアの比較状態が現在スレッドで初期化されていることを保証する。

    方針（案A）:
    - ペアは work/pairs/<a>_<b> に一意に解決する（dirを引数で渡さない）
    - compare_* はグローバル状態（COMPARE_STATE）に依存しているため、
      必要ならこのスレッドで compare_setup を再実行して復元する。
    - initial_matching は pair_dir/initial_matching.json を優先して読み込む。
    """
    a_id = str(doc_a_id or "").strip()
    b_id = str(doc_b_id or "").strip()
    if not a_id or not b_id or a_id == b_id:
        raise ValueError("doc_a_id and doc_b_id must be different and non-empty")
    if a_id not in docs_by_id or b_id not in docs_by_id:
        raise ValueError("unknown doc_id for compare tools")

    pair_dir = work_dir / "pairs" / _safe_pair_key(a_id, b_id)
    if not pair_dir.exists():
        # pair_compare_setup が未実行（または失敗）とみなす
        raise FileNotFoundError(f"pair_dir not found. run pair_compare_setup first: {pair_dir}")

    cache_path = pair_dir / "embedding_cache.json"
    initial_matching_path = pair_dir / "initial_matching.json"

    ast_a_path = _resolve_doc_path(a_id, "ast", docs_by_id, run_dir, work_dir)
    ast_b_path = _resolve_doc_path(b_id, "ast", docs_by_id, run_dir, work_dir)
    doc_a_txt = _resolve_doc_path(a_id, "txt", docs_by_id, run_dir, work_dir)
    doc_b_txt = _resolve_doc_path(b_id, "txt", docs_by_id, run_dir, work_dir)

    if not ast_a_path or not ast_a_path.exists():
        raise FileNotFoundError(str(ast_a_path) if ast_a_path else f"ast not found: {a_id}")
    if not ast_b_path or not ast_b_path.exists():
        raise FileNotFoundError(str(ast_b_path) if ast_b_path else f"ast not found: {b_id}")

    # compare_* tools は src.tools.COMPARE_STATE（スレッドローカル）に依存する。
    # tool が別スレッドで実行される場合があるため、各 tool call の中で必ず復元できるようにする。
    from src.tools import COMPARE_STATE, compare_setup as compare_setup_tool

    wanted_marker = f"{ctx.run_id}:{pair_dir.resolve()}"
    current_marker = str(COMPARE_STATE.get("_pair_marker") or "")

    if current_marker != wanted_marker or COMPARE_STATE.get("ast_a") is None:
        # このスレッドで compare_setup を実行して状態を初期化（cacheがあれば高速）
        # NOTE: compare_setup は内部で COMPARE_STATE.clear() を行う
        compare_setup_tool.invoke(
            {
                "docA": str(ast_a_path),
                "docB": str(ast_b_path),
                "docA_txt": str(doc_a_txt) if doc_a_txt and doc_a_txt.exists() else None,
                "docB_txt": str(doc_b_txt) if doc_b_txt and doc_b_txt.exists() else None,
                "embedding_model": ctx.params.get("embedding_model"),
                "cache_path": str(cache_path),
                "batch_size": int(ctx.params.get("embedding_batch_size", 64)),
            }
        )
        COMPARE_STATE["_run_id"] = ctx.run_id
        COMPARE_STATE["_pair_marker"] = wanted_marker

    # initial_matching をペア成果物から復元（存在すれば）
    if initial_matching_path.exists():
        try:
            initial_text = initial_matching_path.read_text(encoding="utf-8", errors="replace")
            initial_data = json.loads(initial_text)
            if isinstance(initial_data, dict):
                COMPARE_STATE["initial_matching"] = initial_data
        except Exception:
            pass

    return wanted_marker, pair_dir


def _build_pair_scoped_compare_tools(
    ctx: RunContext,
    docs_by_id: dict[str, dict[str, Any]],
    run_dir: Path,
    work_dir: Path,
):
    """
    compare_* をペア指定（doc_a_id/doc_b_id）で必ず動くようにするツール群を返す。

    - 呼び出し側は dir を渡さず、doc_id のペアだけ渡す
    - 内部でペアdirを一意に解決し、必要なら compare_setup を復元する
    """
    from langchain_core.tools import tool

    from src.tools import (
        compare_all_chunk_similarity_matching as _match_all,
        compare_get_chunk as _get_chunk,
        compare_get_grouping as _get_grouping,
        compare_specified_chunks_diff as _specified_diff,
        compare_specified_chunks_llm as _specified_llm,
        COMPARE_STATE,
    )

    def _invoke_tool(tool_func, **kwargs):
        if hasattr(tool_func, "invoke"):
            return tool_func.invoke(kwargs)
        return tool_func(**kwargs)

    @tool
    def compare_all_chunk_similarity_matching(
        doc_a_id: str,
        doc_b_id: str,
        top_k: int = 3,
        alpha: float = 0.3,
        beta: float = 0.4,
        min_score: float = 0.25,
    ) -> str:
        """指定ペアで類似度マッチングを実行（必要なら compare_setup を復元）。"""
        try:
            _, pair_dir = _ensure_compare_state_for_pair(
                ctx,
                docs_by_id=docs_by_id,
                run_dir=run_dir,
                work_dir=work_dir,
                doc_a_id=doc_a_id,
                doc_b_id=doc_b_id,
            )
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False, indent=2)
        res = _invoke_tool(_match_all, top_k=top_k, alpha=alpha, beta=beta, min_score=min_score)
        # last_matching をペアdirへ永続化（スレッドをまたいでも参照できるように）
        try:
            last = COMPARE_STATE.get("last_matching")
            if isinstance(last, dict):
                (pair_dir / "last_matching.json").write_text(
                    json.dumps(last, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        except Exception:
            pass
        return res

    @tool
    def compare_get_grouping(
        doc_a_id: str,
        doc_b_id: str,
        which: str,
        mode: str = "summary",
        chunk_ids: Optional[list[str]] = None,
        min_similarity: Optional[float] = None,
        max_groups: int = 50,
        max_matches: int = 1,
    ) -> str:
        """指定ペアのグルーピング結果を取得（必要なら compare_setup を復元）。"""
        try:
            _, pair_dir = _ensure_compare_state_for_pair(
                ctx,
                docs_by_id=docs_by_id,
                run_dir=run_dir,
                work_dir=work_dir,
                doc_a_id=doc_a_id,
                doc_b_id=doc_b_id,
            )
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False, indent=2)
        # which=last の場合、まずペアdirの永続化を優先して復元（存在すれば）
        if str(which).lower().strip() == "last":
            last_path = pair_dir / "last_matching.json"
            if last_path.exists():
                try:
                    last = json.loads(last_path.read_text(encoding="utf-8", errors="replace"))
                    if isinstance(last, dict):
                        COMPARE_STATE["last_matching"] = last
                except Exception:
                    pass
        return _invoke_tool(
            _get_grouping,
            which=which,
            mode=mode,
            chunk_ids=chunk_ids,
            min_similarity=min_similarity,
            max_groups=max_groups,
            max_matches=max_matches,
        )

    @tool
    def compare_get_chunk(doc_a_id: str, doc_b_id: str, which: str, chunk_id: str) -> str:
        """指定ペアでチャンク内容を取得（必要なら compare_setup を復元）。"""
        try:
            _ensure_compare_state_for_pair(
                ctx,
                docs_by_id=docs_by_id,
                run_dir=run_dir,
                work_dir=work_dir,
                doc_a_id=doc_a_id,
                doc_b_id=doc_b_id,
            )
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False, indent=2)
        return _invoke_tool(_get_chunk, chunk_ids=[chunk_id], intent="compare_get_chunk", in_doc=which)

    @tool
    def compare_specified_chunks_diff(
        doc_a_id: str,
        doc_b_id: str,
        chunk_id_a: str,
        chunk_id_b: str,
        include_metadata: bool = True,
    ) -> str:
        """指定ペアで2チャンクの差分を取得（必要なら compare_setup を復元）。"""
        try:
            _ensure_compare_state_for_pair(
                ctx,
                docs_by_id=docs_by_id,
                run_dir=run_dir,
                work_dir=work_dir,
                doc_a_id=doc_a_id,
                doc_b_id=doc_b_id,
            )
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False, indent=2)
        return _invoke_tool(
            _specified_diff,
            chunk_id_a=chunk_id_a,
            chunk_id_b=chunk_id_b,
            include_metadata=include_metadata,
        )

    @tool
    def compare_specified_chunks_llm(
        doc_a_id: str,
        doc_b_id: str,
        chunk_id_a: str,
        chunk_id_b: str,
        focus: str = "",
    ) -> str:
        """指定ペアで2チャンクをLLMで比較（必要なら compare_setup を復元）。"""
        try:
            _ensure_compare_state_for_pair(
                ctx,
                docs_by_id=docs_by_id,
                run_dir=run_dir,
                work_dir=work_dir,
                doc_a_id=doc_a_id,
                doc_b_id=doc_b_id,
            )
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False, indent=2)
        return _invoke_tool(_specified_llm, chunk_id_a=chunk_id_a, chunk_id_b=chunk_id_b, focus=focus)

    return [
        compare_all_chunk_similarity_matching,
        compare_get_grouping,
        compare_get_chunk,
        compare_specified_chunks_diff,
        compare_specified_chunks_llm,
    ]


def _extract_json_block(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError("Failed to parse JSON from LLM response")


def _run_with_cancellation(ctx: RunContext, *, label: str, func) -> Any:
    done = threading.Event()
    result: dict[str, Any] = {}
    error: dict[str, Exception] = {}

    def _target() -> None:
        try:
            result["value"] = func()
        except Exception as exc:
            error["error"] = exc
        finally:
            done.set()

    thread = threading.Thread(target=_target, name=f"{label}_worker", daemon=True)
    thread.start()

    while not done.is_set():
        if ctx.cancellation.is_cancelled():
            raise CancelledError(f"cancelled during {label}")
        done.wait(0.2)

    if "error" in error:
        raise error["error"]
    return result.get("value")


def _extract_last_ai_text(result: Any) -> str:
    """deepagents/langgraphのstate(dict)から最終AI出力テキストを取り出す。"""
    if isinstance(result, dict):
        msgs = result.get("messages") or []
        for m in reversed(list(msgs)):
            # langchain_core.messages.AIMessage を想定
            if hasattr(m, "__class__") and m.__class__.__name__ == "AIMessage":
                content = getattr(m, "content", None)
                if content:
                    return str(content)
            # dict形式も許容
            if isinstance(m, dict) and (m.get("role") in {"assistant", "ai"}):
                c = m.get("content")
                if c:
                    return str(c)
    return ""


def _extract_interrupt_payload(result: Any) -> Optional[dict[str, Any]]:
    """deepagents(HITL)の割り込みpayloadを抽出する。"""
    if not isinstance(result, dict):
        return None
    intr = result.get("__interrupt__")
    if not intr:
        return None
    try:
        first = intr[0]
        if hasattr(first, "value"):
            val = getattr(first, "value")
            return val if isinstance(val, dict) else {"value": val}
        if isinstance(first, dict):
            return first
        return {"value": first}
    except Exception:
        return None


def _vfile_to_text(fd: Any) -> Optional[str]:
    """deepagentsの files 互換: {path: str} または {path: {"content": ...}} を吸収。"""
    if fd is None:
        return None
    if isinstance(fd, str):
        return fd
    if isinstance(fd, dict) and "content" in fd:
        raw = fd.get("content")
        return "\n".join(raw) if isinstance(raw, list) else str(raw)
    return None


@dataclass
class PreAnalysisStep:
    """関係性判定（Pre-Analysis）とテンプレ生成を行い、work/template_draft.md を作る。"""

    name: str = "pre_analysis"

    def should_run(self, ctx: RunContext) -> bool:
        work_dir = Path(ctx.paths["work_dir"])
        out_draft = work_dir / "template_draft.md"
        force = bool(ctx.params.get("force", False))
        return force or (not out_draft.exists())

    def _run_ndoc(self, ctx: RunContext) -> None:
        work_dir = Path(ctx.paths["work_dir"])
        out_draft = work_dir / "template_draft.md"
        out_meta = work_dir / "pre_analysis.json"
        run_dir = Path(ctx.paths["run_dir"])
        config = _load_run_config(ctx)
        docs = _get_documents(ctx)
        if len(docs) < 1:
            raise ValueError("pre_analysis requires at least 1 document")

        request_text = ctx.params.get("request_text") or config.get("request_text")
        if not request_text or not str(request_text).strip():
            raise ValueError("request_text is required for pre_analysis")
        request_text = str(request_text).strip()

        hil_enabled = bool(
            ctx.params.get("hil_enabled")
            if "hil_enabled" in ctx.params
            else config.get("hil_enabled", False)
        )

        # テンプレ seed（Runに紐付いたテンプレート）を優先して使用する。
        # - Run作成時に input/template_seed.(md|txt) が作られていれば、それを読む
        template_seed_text: Optional[str] = None
        template_seed_path_raw = ctx.params.get("template_seed_path") or config.get("template_seed_path")
        if template_seed_path_raw and not template_seed_text:
            try:
                seed_path = Path(str(template_seed_path_raw))
                seed_abs = seed_path if seed_path.is_absolute() else (run_dir / seed_path)
                if seed_abs.exists():
                    template_seed_text = seed_abs.read_text(encoding="utf-8", errors="replace")
            except Exception:
                template_seed_text = None

        if not _have_llm_key():
            raise RuntimeError("LLM API key not set (OPENAI_API_KEY or AZURE_OPENAI_API_KEY)")

        docs_meta: list[dict[str, Any]] = []
        for d in docs:
            did = str(d.get("doc_id"))
            fname = d.get("filename") or d.get("original_filename") or ""
            txt_path = work_dir / f"doc_{did}.txt"
            bp_path = work_dir / f"blueprint_{did}.json"
            ast_path = work_dir / f"ast_{did}.ast.json"
            if not txt_path.exists():
                raise FileNotFoundError(str(txt_path))
            docs_meta.append(
                {
                    "doc_id": did,
                    "filename": str(fname) if fname else None,
                    "source": d.get("source"),
                    "role_guess": d.get("role_guess"),
                    "role_reason": d.get("role_reason"),
                    "confidence": d.get("confidence"),
                    "paths": {
                        "txt": txt_path.relative_to(run_dir).as_posix() if txt_path.exists() else None,
                        "blueprint": bp_path.relative_to(run_dir).as_posix() if bp_path.exists() else None,
                        "ast": ast_path.relative_to(run_dir).as_posix() if ast_path.exists() else None,
                    },
                }
            )

        docs_by_id: dict[str, dict[str, Any]] = {str(d.get("doc_id")): d for d in docs_meta if d.get("doc_id")}

        docs_dir = work_dir / "docs"
        docs_dir.mkdir(parents=True, exist_ok=True)
        docs_index_path = docs_dir / "index.json"
        docs_index_data = {"request_text": request_text, "documents": docs_meta}
        docs_index_path.write_text(json.dumps(docs_index_data, ensure_ascii=False, indent=2), encoding="utf-8")

        request_path = work_dir / "request.txt"
        request_path.write_text(request_text, encoding="utf-8")

        try:
            ctx.events.emit(
                ctx.run_id,
                "artifact_updated",
                {"ts": _utcnow_iso(), "kind": "docs_index", "path": docs_index_path.relative_to(run_dir).as_posix()},
            )
            ctx.events.emit(
                ctx.run_id,
                "artifact_updated",
                {"ts": _utcnow_iso(), "kind": "request_text", "path": request_path.relative_to(run_dir).as_posix()},
            )
        except Exception:
            pass

        from deepagents import create_deep_agent
        from deepagents.backends.utils import create_file_data
        from deepagents.backends.utils import create_file_data
        from deepagents.backends.utils import create_file_data

        from src.prompt import task_pre_analysis_prompt
        from src.schema import TaskPreAnalysisResult
        from src.tools import (
            extract_regex_matches,
            get_file_length,
            read_ast,
            read_text_file,
            read_text_segment,
            search_by_keyphrase,
        )
        from src.utils import build_llm

        from langgraph.types import Command

        from document_process_app.agents.hitl import (
            get_hitl_paths,
            human_input,
            load_json_if_exists,
            open_checkpointer,
            remove_if_exists,
            write_json,
        )

        llm_complex = build_llm(model=str(ctx.params.get("llm_complex_model", "gpt-5-mini")))

        pair_compare_setup = _build_pair_compare_setup_tool(ctx, docs_by_id, run_dir, work_dir)

        tools: list[Any] = [
            read_ast,
            read_text_file,
            read_text_segment,
            extract_regex_matches,
            get_file_length,
            pair_compare_setup,
        ]

        interrupt_config = None
        if hil_enabled:
            tools.append(human_input)
            interrupt_config = {"human_input": True}

        recursion_limit = int(ctx.params.get("recursion_limit", 120) or 120)
        config_lg = {"configurable": {"thread_id": str(ctx.run_id)}, "recursion_limit": recursion_limit}
        interrupt_path, resume_path = get_hitl_paths(work_dir)

        files: dict[str, Any] = {
            "/request.txt": create_file_data(str(request_text)),
            "/docs/index.json": create_file_data(docs_index_path.read_text(encoding="utf-8", errors="replace")),
        }
        if template_seed_text is not None:
            files["/template_draft.md"] = create_file_data(str(template_seed_text))

        for d in docs:
            did = str(d.get("doc_id"))
            txt_path = work_dir / f"doc_{did}.txt"
            bp_path = work_dir / f"blueprint_{did}.json"
            ast_path = work_dir / f"ast_{did}.ast.json"
            if txt_path.exists():
                files[f"/docs/{did}/doc.txt"] = create_file_data(txt_path.read_text(encoding="utf-8", errors="replace"))
            if bp_path.exists():
                files[f"/docs/{did}/blueprint.json"] = create_file_data(bp_path.read_text(encoding="utf-8", errors="replace"))
            if ast_path.exists():
                files[f"/docs/{did}/ast.json"] = create_file_data(ast_path.read_text(encoding="utf-8", errors="replace"))

        query_lines = [
            "依頼文とドキュメント一覧を読み、タスク計画とテンプレートを生成してください。",
            "- 依頼文: /request.txt",
            "- ドキュメント一覧: /docs/index.json",
            f"- HIL: {'enabled' if hil_enabled else 'disabled'}",
            "必要に応じて /docs/<doc_id>/doc.txt /ast.json /blueprint.json を参照してください。",
            "比較準備が必要な場合は pair_compare_setup(doc_a_id, doc_b_id, purpose) を呼び出してください。",
        ]
        if template_seed_text is not None:
            query_lines.append("テンプレートは /template_draft.md として提供されています。必要に応じて改稿してください。")
        if hil_enabled:
            query_lines.extend(
                [
                    "",
                    "不足情報や判断保留がある場合は human_input(question, answer=\"\") を呼び出して人間に確認してください（HITL）。",
                ]
            )
        query = "\n".join(query_lines)

        with open_checkpointer() as checkpointer:
            middleware = []
            try:
                from document_process_app.agents.middleware import EventSinkMiddleware  # type: ignore

                middleware = [
                    EventSinkMiddleware(
                        run_id=ctx.run_id,
                        events=ctx.events,
                        cancellation=ctx.cancellation,
                        agent_name="task_pre_analysis_agent",
                    )
                ]
            except Exception:
                # 環境によっては langchain.agents.middleware が無い等で middleware import が失敗する。
                # その場合でも実行自体は継続する（UIの詳細ログは出ない）。
                middleware = []

            agent = create_deep_agent(
                model=llm_complex,
                tools=tools,
                system_prompt=task_pre_analysis_prompt,
                interrupt_on=interrupt_config,
                checkpointer=checkpointer,
                middleware=middleware,
                debug=False,
            )

            resume_decisions = load_json_if_exists(resume_path) if hil_enabled else None
            if isinstance(resume_decisions, dict) and "decisions" in resume_decisions:
                resume_decisions = resume_decisions.get("decisions")

            if hil_enabled and isinstance(resume_decisions, list) and resume_decisions:
                input_obj: Any = Command(resume={"decisions": resume_decisions})
            else:
                input_obj = {"messages": [{"role": "user", "content": query}], "files": files}

            result = _run_with_cancellation(
                ctx,
                label="task_pre_analysis_agent",
                func=lambda: agent.invoke(input_obj, config=config_lg),
            )

        # resumeファイルはここで消費（次回の誤再開防止）
        if hil_enabled:
            remove_if_exists(resume_path)

        interrupt_payload = _extract_interrupt_payload(result)
        if hil_enabled and interrupt_payload:
            # UIが質問を表示できるよう、work配下に保存
            write_json(interrupt_path, interrupt_payload)
            try:
                ctx.events.emit(
                    ctx.run_id,
                    "hitl_interrupt",
                    {
                        "ts": _utcnow_iso(),
                        "step": self.name,
                        "action_requests": interrupt_payload.get("action_requests"),
                    },
                )
            except Exception:
                pass
            from document_process_app.core.pipeline import WaitingUserError

            raise WaitingUserError("hitl interrupted (pre_analysis)")

        # 正常復帰したら、古いinterruptファイルは消す
        if hil_enabled:
            remove_if_exists(interrupt_path)

        last_text = _extract_last_ai_text(result)
        if not last_text.strip():
            raise RuntimeError("pre_analysis_agent returned no final AI message")
        structured_dict = _extract_json_block(last_text)
        try:
            structured = TaskPreAnalysisResult(**structured_dict)
        except Exception as e:
            raise RuntimeError(f"pre_analysis_agent returned invalid JSON: {e}")

        needs_review = False
        review_questions: list[str] = []

        vfiles = result.get("files") or {}

        if hil_enabled:
            review_fd = vfiles.get("/needs_review.json") or vfiles.get("needs_review.json")
            review_text = _vfile_to_text(review_fd)
            if review_text:
                try:
                    review_data = json.loads(review_text)
                    if isinstance(review_data, dict):
                        needs_review = bool(review_data.get("needs_review", False))
                        questions = review_data.get("questions") or []
                        if isinstance(questions, list):
                            review_questions = [str(q) for q in questions if str(q).strip()]
                except Exception:
                    raise RuntimeError("Invalid /needs_review.json format")
        else:
            if "/needs_review.json" in vfiles or "needs_review.json" in vfiles:
                raise RuntimeError("needs_review.json provided but hil_enabled=false")

        draft_fd = vfiles.get("/template_draft.md") or vfiles.get("template_draft.md")
        template_text: Optional[str] = None
        template_text = _vfile_to_text(draft_fd)
        if not template_text and template_seed_text is not None:
            template_text = template_seed_text
        if not template_text:
            # execute が常に template_draft.md を必要とするため、最低限のテンプレを必ず用意する
            template_text = "\n".join(
                [
                    "# レポート（テンプレ）",
                    "",
                    "## 出力",
                    "- （ここに結果を記載）",
                    "",
                ]
            )

        is_complete = bool(getattr(structured, "is_complete", False))
        filled_report = getattr(structured, "filled_report", "") or ""

        if is_complete and not filled_report:
            filled_fd = vfiles.get("/template_filled.md") or vfiles.get("template_filled.md")
            filled_report = _vfile_to_text(filled_fd) or filled_report

        if is_complete and not filled_report:
            raise RuntimeError("is_complete=True but filled_report is empty")
        # template_text は常に埋まる（seed/agent/フォールバックのいずれか）

        if is_complete and filled_report:
            out_dir = Path(ctx.paths["out_dir"])
            out_dir.mkdir(parents=True, exist_ok=True)
            filled_path = out_dir / "template_filled.md"
            filled_path.write_text(filled_report, encoding="utf-8")

            try:
                ctx.events.emit(
                    ctx.run_id,
                    "artifact_updated",
                    {"ts": _utcnow_iso(), "kind": "template_filled", "path": "out/template_filled.md"},
                )
            except Exception:
                pass

        if template_text is not None:
            out_draft.write_text(template_text or "", encoding="utf-8")
            try:
                ctx.events.emit(
                    ctx.run_id,
                    "artifact_updated",
                    {"ts": _utcnow_iso(), "kind": "template_draft", "path": out_draft.relative_to(run_dir).as_posix()},
                )
            except Exception:
                pass

        def _as_dict(obj: Any) -> Optional[dict[str, Any]]:
            if isinstance(obj, dict):
                return obj
            if hasattr(obj, "model_dump"):
                return obj.model_dump()  # type: ignore[no-any-return]
            if hasattr(obj, "dict"):
                return obj.dict()  # type: ignore[no-any-return]
            return None

        structured_docs = list(getattr(structured, "documents", []) or [])
        if not structured_docs:
            raise RuntimeError("documents is required in pre_analysis result")

        meta_by_id = {str(d.get("doc_id")): d for d in docs_meta}
        structured_by_id: dict[str, Any] = {}
        for sd in structured_docs:
            sd_dict = _as_dict(sd)
            if not sd_dict:
                continue
            did = str(sd_dict.get("doc_id") or "")
            if not did:
                continue
            if did not in meta_by_id:
                raise RuntimeError(f"Unknown doc_id in pre_analysis result: {did}")
            structured_by_id[did] = sd_dict

        documents_out: list[dict[str, Any]] = []
        for did, meta in meta_by_id.items():
            sd = structured_by_id.get(did, {})
            merged = dict(meta)
            if sd:
                merged["role_guess"] = sd.get("role_guess", merged.get("role_guess"))
                merged["role_reason"] = sd.get("role_reason", merged.get("role_reason"))
                merged["confidence"] = sd.get("confidence", merged.get("confidence"))
                merged["filename"] = sd.get("filename") or merged.get("filename")
            documents_out.append(merged)

        task_type = str(getattr(structured, "task_type", "") or "").strip() or None
        execution_plan_raw = getattr(structured, "execution_plan", None)
        execution_plan: list[dict[str, Any]] = []
        template_name = getattr(structured, "template", None)

        if not execution_plan_raw:
            raise RuntimeError("execution_plan is required in pre_analysis result")
        for step in execution_plan_raw:
            step_dict = _as_dict(step)
            if not step_dict:
                raise RuntimeError("invalid execution_plan format in pre_analysis result")
            execution_plan.append(step_dict)
        if not template_name:
            raise RuntimeError("template is required in pre_analysis result")

        out_meta.write_text(
            json.dumps(
                {
                    "schema_version": "pre_analysis.v2",
                    "request_text": request_text,
                    "hil_enabled": hil_enabled,
                    "documents": documents_out,
                    "task_type": task_type,
                    "execution_plan": execution_plan,
                    "template_name": template_name,
                    "is_complete": is_complete,
                    "filled_report": filled_report if is_complete else None,
                    "completion": {
                        "is_complete": is_complete,
                        "needs_review": needs_review,
                        "questions": review_questions,
                        "reason": "pre_analysis_is_complete" if is_complete else "",
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        ctx.events.emit(
            ctx.run_id,
            "artifact_updated",
            {"ts": _utcnow_iso(), "kind": "pre_analysis", "path": out_meta.relative_to(run_dir).as_posix()},
        )

        if needs_review and hil_enabled:
            ctx.events.emit(
                ctx.run_id,
                "waiting_user_requested",
                {"ts": _utcnow_iso(), "step": self.name, "questions": review_questions},
            )
            from document_process_app.core.pipeline import WaitingUserError

            raise WaitingUserError("pre_analysis requested human review")

    def run(self, ctx: RunContext) -> None:
        self._run_ndoc(ctx)
        return

@dataclass
class CompareAnalysisStep:
    """Pre-Analysisで作ったテンプレを埋めて、out/template_filled.md を作る。"""

    name: str = "execute_analysis"

    def should_run(self, ctx: RunContext) -> bool:
        work_dir = Path(ctx.paths["work_dir"])
        out_dir = Path(ctx.paths["out_dir"])
        filled = out_dir / "template_filled.md"
        force = bool(ctx.params.get("force", False))
        return force or (not filled.exists())

    def _run_ndoc_execute(self, ctx: RunContext) -> None:
        work_dir = Path(ctx.paths["work_dir"])
        out_dir = Path(ctx.paths["out_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        run_dir = Path(ctx.paths["run_dir"])

        pre_analysis_path = work_dir / "pre_analysis.json"
        if not pre_analysis_path.exists():
            raise FileNotFoundError(str(pre_analysis_path))
        try:
            pre_analysis_data = json.loads(pre_analysis_path.read_text(encoding="utf-8"))
        except Exception:
            raise RuntimeError("failed to read pre_analysis.json")

        # pre_analysis で完結した場合: out/template_filled.md が必須
        if bool(pre_analysis_data.get("is_complete", False)):
            filled_path = out_dir / "template_filled.md"
            if not filled_path.exists():
                raise RuntimeError("pre_analysis is_complete=true but out/template_filled.md is missing")
            return

        request_text = ctx.params.get("request_text") or pre_analysis_data.get("request_text")
        if not request_text or not str(request_text).strip():
            raise ValueError("request_text is required for execute_task")
        request_text = str(request_text).strip()

        docs_index_path = work_dir / "docs" / "index.json"
        if not docs_index_path.exists():
            raise FileNotFoundError(str(docs_index_path))

        template_path = work_dir / "template_draft.md"
        if not template_path.exists():
            raise FileNotFoundError(str(template_path))

        documents = pre_analysis_data.get("documents")
        if not isinstance(documents, list) or not documents:
            raise RuntimeError("documents is required for execute_task")

        docs_by_id: dict[str, dict[str, Any]] = {}
        for d in documents:
            if isinstance(d, dict) and d.get("doc_id"):
                docs_by_id[str(d.get("doc_id"))] = d
        if not docs_by_id:
            raise RuntimeError("no valid documents found for execute_task")

        pair_compare_setup = _build_pair_compare_setup_tool(ctx, docs_by_id, run_dir, work_dir)
        pair_scoped_compare_tools = _build_pair_scoped_compare_tools(ctx, docs_by_id, run_dir, work_dir)

        from deepagents import create_deep_agent
        from deepagents.backends.utils import create_file_data

        from src.tools import (
            extract_regex_matches,
            get_file_length,
            read_ast,
            read_text_file,
            read_text_segment,
            search_by_keyphrase,
        )
        from src.utils import build_llm

        llm_complex = build_llm(model=str(ctx.params.get("llm_complex_model", "gpt-5-mini")))
        llm_visual = build_llm(
            model=str(ctx.params.get("llm_visual_model", ctx.params.get("llm_complex_model", "gpt-5-mini")))
        )
        from langchain_core.messages import HumanMessage
        from langchain_core.tools import tool
        import base64
        import fitz  # PyMuPDF

        input_dir = Path(ctx.paths["input_dir"])

        def _get_pdf_page_as_image(pdf_path: Path, page_numbers: list[int], dpi: int = 150) -> list[dict]:
            doc = fitz.open(str(pdf_path))
            try:
                out = []
                for page_number in page_numbers:
                    if page_number < 1 or page_number > len(doc):
                        raise ValueError(f"invalid page {page_number} (total {len(doc)})")
                    page = doc[page_number - 1]
                    mat = fitz.Matrix(dpi / 72, dpi / 72)
                    pix = page.get_pixmap(matrix=mat)
                    img_bytes = pix.tobytes("png")
                    b64_data = base64.b64encode(img_bytes).decode("utf-8")
                    out.append({"page_number": page_number, "base64_data": b64_data})
                return out
            finally:
                doc.close()

        @tool
        def analyze_visual_contents(document_name: str, page_numbers: list[int], prompt: str) -> str:
            """
            Run入力のPDF（data/runs/{run_id}/input）を対象に、指定ページを画像として分析して返す。
            document_name が .txt/.ast.json の場合は .pdf に置換して探索する。
            """
            name = str(document_name or "").strip()
            if name.endswith(".ast.json"):
                name = name.replace(".ast.json", ".pdf")
            elif name.endswith(".txt"):
                name = name.replace(".txt", ".pdf")
            p = Path(name)
            if p.is_absolute() and p.exists():
                pdf_path = p
            else:
                cand = input_dir / name
                if cand.exists():
                    pdf_path = cand
                else:
                    pdf_candidates = sorted(input_dir.glob("*.pdf"))
                    if pdf_candidates:
                        pdf_path = pdf_candidates[0]
                    else:
                        raise FileNotFoundError(f"pdf not found: {name}")

            pages = _get_pdf_page_as_image(pdf_path, page_numbers, dpi=150)
            if not pages:
                raise RuntimeError("failed to render pdf pages")

            prompt_lines = [
                prompt or "PDFの視覚情報を分析し、表や図の内容を説明してください。",
            ]
            messages = [HumanMessage(content="\n".join(prompt_lines))]
            for page in pages:
                messages.append(
                    HumanMessage(
                        content=[
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{page['base64_data']}"
                                },
                            },
                            {
                                "type": "text",
                                "text": f"page {page['page_number']}",
                            },
                        ]
                    )
                )

            resp = llm_visual.invoke(messages)
            return getattr(resp, "content", None) or str(resp)

        tools = [
            read_ast,
            read_text_file,
            read_text_segment,
            extract_regex_matches,
            get_file_length,
            search_by_keyphrase,
            *pair_scoped_compare_tools,
            analyze_visual_contents,
            pair_compare_setup,
        ]

        executor_prompt = "\n".join(
            [
                "あなたはタスク実行エージェントです。/pre_analysis.json の execution_plan に従って実行してください。",
                "- 必要な比較準備は pair_compare_setup(doc_a_id, doc_b_id, purpose) を呼び出すこと",
                "- compare_* ツールは doc_a_id/doc_b_id のペア指定が必須です（dirは指定しません）",
                "- compare_* ツールを使う前に、対象ペアの pair_compare_setup を必ず実行すること（未実行の場合は失敗します）",
                "- 最終成果物は /template_filled.md として出力すること",
                "- **重要**: 出力はMarkdown形式で記述すること（見出しは `#`, `##`, `###`、箇条書きは `-`、番号付きリストは `1.` 等）",
                "",
                "利用可能なファイル:",
                "- /template_draft.md (テンプレート)",
                "- /pre_analysis.json (計画)",
                "- /docs/index.json (ドキュメント一覧)",
                "- /docs/<doc_id>/doc.txt (原文)",
                "- /docs/<doc_id>/ast.json (AST, 任意)",
                "- /docs/<doc_id>/blueprint.json (Blueprint, 任意)",
                "- /request.txt (依頼文)",
            ]
        )

        middleware = []
        try:
            from document_process_app.agents.middleware import EventSinkMiddleware  # type: ignore

            middleware = [
                EventSinkMiddleware(
                    run_id=ctx.run_id,
                    events=ctx.events,
                    cancellation=ctx.cancellation,
                    agent_name="task_executor_agent",
                )
            ]
        except Exception:
            middleware = []

        agent = create_deep_agent(
            model=llm_complex,
            tools=tools,
            system_prompt=executor_prompt,
            middleware=middleware,
            debug=False,
        )

        files: dict[str, Any] = {
            "/template_draft.md": create_file_data(template_path.read_text(encoding="utf-8", errors="replace")),
            "/pre_analysis.json": create_file_data(pre_analysis_path.read_text(encoding="utf-8", errors="replace")),
            "/docs/index.json": create_file_data(docs_index_path.read_text(encoding="utf-8", errors="replace")),
            "/request.txt": create_file_data(str(request_text)),
        }

        for doc_id, meta in docs_by_id.items():
            txt_path = _resolve_doc_path(doc_id, "txt", docs_by_id, run_dir, work_dir)
            bp_path = _resolve_doc_path(doc_id, "blueprint", docs_by_id, run_dir, work_dir)
            ast_path = _resolve_doc_path(doc_id, "ast", docs_by_id, run_dir, work_dir)
            if txt_path and txt_path.exists():
                files[f"/docs/{doc_id}/doc.txt"] = create_file_data(txt_path.read_text(encoding="utf-8", errors="replace"))
            if bp_path and bp_path.exists():
                files[f"/docs/{doc_id}/blueprint.json"] = create_file_data(bp_path.read_text(encoding="utf-8", errors="replace"))
            if ast_path and ast_path.exists():
                files[f"/docs/{doc_id}/ast.json"] = create_file_data(ast_path.read_text(encoding="utf-8", errors="replace"))

        recursion_limit = int(ctx.params.get("recursion_limit", 120) or 120)
        config_lg = {"configurable": {"thread_id": str(ctx.run_id)}, "recursion_limit": recursion_limit}

        result = _run_with_cancellation(
            ctx,
            label="task_executor_agent",
            func=lambda: agent.invoke({"messages": [{"role": "user", "content": "execute"}], "files": files}, config=config_lg),
        )

        vfiles = result.get("files") or {}
        content: Optional[str] = None
        for key in ["/template_filled.md", "template_filled.md", "/out/template_filled.md"]:
            fd = vfiles.get(key)
            text = _vfile_to_text(fd)
            if text:
                content = text
                break
        if not content:
            raise RuntimeError("template_filled.md not produced by executor")

        filled_path = out_dir / "template_filled.md"
        filled_path.write_text(content, encoding="utf-8")
        ctx.events.emit(
            ctx.run_id,
            "artifact_updated",
            {"ts": _utcnow_iso(), "kind": "template_filled", "path": "out/template_filled.md"},
        )
        return

    def run(self, ctx: RunContext) -> None:
        self._run_ndoc_execute(ctx)
        return
