from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

from document_process_app.bootstrap import build_default_executor


def _print_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="document_process_agent",
        description="document_process_agent app CLI (UI/CLI共通RunExecutorを使用)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create", help="Runを作成する")
    p_create.add_argument("--doc", action="append", default=[], help="ドキュメントのパス（複数指定可）")
    p_create.add_argument("--doc-hash", action="append", default=[], help="既存ドキュメントのhash（複数指定可）")
    p_create.add_argument("--request", default="", help="依頼文（pre_analysisで使用）")
    p_create.add_argument("--hil", action="store_true", help="Human-in-the-loopを有効化（必要時のみ停止）")
    p_create.add_argument("--mode", choices=["dummy", "real"], default="dummy", help="パイプラインモード")
    p_create.add_argument("--params", default="{}", help="params JSON（例: '{\"mode\":\"dummy\"}'）")

    p_start = sub.add_parser("start", help="Runをバックグラウンド実行で開始する（in-process）")
    p_start.add_argument("run_id")

    p_execute = sub.add_parser("execute", help="Runを同期実行する（テスト/CI向け）")
    p_execute.add_argument("run_id")

    p_cancel = sub.add_parser("cancel", help="Runに協調的キャンセル要求を出す")
    p_cancel.add_argument("run_id")

    p_tail = sub.add_parser("tail", help="Runイベントを追跡表示する（SSE代替）")
    p_tail.add_argument("run_id")
    p_tail.add_argument("--poll", type=float, default=0.5)

    p_list = sub.add_parser("list", help="Run一覧")
    p_list.add_argument("--limit", type=int, default=20)

    p_artifacts = sub.add_parser("artifacts", help="Runの成果物一覧（DB優先）")
    p_artifacts.add_argument("run_id")

    p_export = sub.add_parser("export", help="成果物をファイルへ書き出す（例: template_filled）")
    p_export.add_argument("run_id")
    p_export.add_argument("--kind", required=True, help="artifact kind（例: template_filled, events_log_jsonl）")
    p_export.add_argument("--out", required=True, help="出力先パス")

    args = parser.parse_args(argv)
    executor, repo, events, artifacts_repo = build_default_executor()

    if args.cmd == "create":
        try:
            params = json.loads(args.params)
        except Exception:
            params = {}
        if not isinstance(params, dict):
            params = {"params": params}
        params.setdefault("mode", args.mode)
        # デフォルトで AST 枝サマリを有効化（run跨ぎで要約済みASTを確実に再利用するため）
        # ※ dummy モードでは SummarizeAstStep 自体が実行されないので安全
        params.setdefault("summarize_ast", True)
        documents: list[dict[str, str]] = []
        for p in args.doc:
            documents.append({"path": str(p)})
        for h in args.doc_hash:
            documents.append({"doc_hash": str(h)})
        if len(documents) < 1:
            _print_json({"ok": False, "error": "documents must be >= 1 (use --doc/--doc-hash)"})
            return 2
        run = executor.create_run(
            documents=documents,
            request_text=str(args.request or ""),
            hil_enabled=bool(args.hil),
            params=params,
        )
        _print_json({"run_id": run.run_id, "status": run.status, "created_at": run.created_at.isoformat()})
        return 0

    if args.cmd == "start":
        job_id = executor.start(args.run_id)
        _print_json({"ok": True, "run_id": args.run_id, "job_id": job_id})
        return 0

    if args.cmd == "cancel":
        executor.request_cancel(args.run_id)
        _print_json({"ok": True, "run_id": args.run_id})
        return 0

    if args.cmd == "execute":
        try:
            executor.execute(args.run_id)
            _print_json({"ok": True, "run_id": args.run_id})
            return 0
        except Exception as e:
            _print_json({"ok": False, "run_id": args.run_id, "error": str(e), "error_type": type(e).__name__})
            return 1

    if args.cmd == "tail":
        last_id = None
        while True:
            evs = events.list(args.run_id, after_event_id=last_id, limit=200)
            if evs:
                for ev in evs:
                    last_id = ev.event_id
                    print(f"[{ev.event_id}] {ev.ts.isoformat()} {ev.event_type} {ev.payload}")
            time.sleep(max(0.1, float(args.poll)))

    if args.cmd == "list":
        runs = repo.list_runs(limit=args.limit)
        _print_json(
            [
                {
                    "run_id": r.run_id,
                    "status": r.status,
                    "created_at": r.created_at.isoformat(),
                    "started_at": r.started_at.isoformat() if r.started_at else None,
                    "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                }
                for r in runs
            ]
        )
        return 0

    if args.cmd == "artifacts":
        items = artifacts_repo.list_artifacts(args.run_id)
        _print_json(items)
        return 0

    if args.cmd == "export":
        run = repo.get_run(args.run_id)
        run_dir = Path(run.workdir) if run.workdir else (Path("data") / "runs" / args.run_id)

        src_rel = None
        for it in artifacts_repo.list_artifacts(args.run_id):
            if str(it.get("kind")) == str(args.kind):
                src_rel = str(it.get("path"))
                break
        # fallback（最低限）
        if not src_rel:
            if args.kind == "template_filled":
                src_rel = "out/template_filled.md"
            elif args.kind == "template_draft":
                src_rel = "work/template_draft.md"
            elif args.kind == "events_log_jsonl":
                src_rel = "log/events.jsonl"

        if not src_rel:
            _print_json({"ok": False, "error": f"artifact not found for kind: {args.kind}"})
            return 1

        src = (run_dir / src_rel)
        if not src.exists():
            _print_json({"ok": False, "error": f"artifact file not found: {src}"})
            return 1

        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # バイナリもあり得るのでbytesコピー
        out_path.write_bytes(src.read_bytes())

        _print_json({"ok": True, "run_id": args.run_id, "kind": args.kind, "src": str(src), "out": str(out_path)})
        return 0

    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

