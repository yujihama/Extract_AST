from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from document_process_app.core.pipeline import CancelledError, RunContext


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class DummySleepStep:
    name: str
    seconds: float = 1.0
    tick: float = 0.2

    def run(self, ctx: RunContext) -> None:
        end = time.time() + float(self.seconds)
        while time.time() < end:
            if ctx.cancellation.is_cancelled():
                raise CancelledError("cancelled")
            ctx.events.emit(ctx.run_id, "heartbeat", {"ts": _utcnow_iso(), "step": self.name})
            time.sleep(max(0.05, float(self.tick)))


@dataclass
class DummyWriteTemplateDraftStep:
    name: str = "dummy_write_template_draft"

    def run(self, ctx: RunContext) -> None:
        run_dir = Path(ctx.paths["run_dir"])
        work_dir = Path(ctx.paths["work_dir"])
        work_dir.mkdir(parents=True, exist_ok=True)
        p = work_dir / "template_draft.md"
        p.write_text(
            "\n".join(
                [
                    "# 生成テンプレ（ダミー）",
                    "",
                    "- relation:",
                    "- reason:",
                    "- plan:",
                    "",
                    "## 差分まとめ",
                    "",
                    "(ここに段階的に追記されます)",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        rel = p.relative_to(run_dir).as_posix()
        ctx.events.emit(ctx.run_id, "artifact_updated", {"ts": _utcnow_iso(), "kind": "template_draft", "path": rel})


@dataclass
class DummyFillTemplateStep:
    name: str = "dummy_fill_template"

    def run(self, ctx: RunContext) -> None:
        run_dir = Path(ctx.paths["run_dir"])
        work_dir = Path(ctx.paths["work_dir"])
        out_dir = Path(ctx.paths["out_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)

        draft = work_dir / "template_draft.md"
        if not draft.exists():
            raise FileNotFoundError(str(draft))

        filled = out_dir / "template_filled.md"
        base = draft.read_text(encoding="utf-8", errors="replace")
        filled.write_text(
            base
            + "\n"
            + "\n".join(
                [
                    "## 実行結果（ダミー）",
                    "",
                    "- relation: Revision",
                    "- reason: ダミー実行のため固定",
                    "- plan: ['step1','step2']",
                    "",
                    "### 差分（例）",
                    "- 追加: Aにない項目がBに追加",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        rel = filled.relative_to(run_dir).as_posix()
        ctx.events.emit(ctx.run_id, "artifact_updated", {"ts": _utcnow_iso(), "kind": "template_filled", "path": rel})


@dataclass
class DummyAgentTraceStep:
    """deep_agentの可視化（agent/toolイベント）をdummyで疑似発火する。"""

    name: str = "dummy_agent_trace"

    def run(self, ctx: RunContext) -> None:
        import time

        # ダミー用の固定invocation_id
        parent_inv_id = "dummy_parent1"
        child_inv_id = "dummy_child01"

        ts = _utcnow_iso()
        # 親エージェント開始
        ctx.events.emit(ctx.run_id, "agent_start", {
            "ts": ts,
            "agent_name": "dummy_agent",
            "is_subagent": False,
            "invocation_id": parent_inv_id,
            "parent_invocation_id": None,
            "parent_agent_name": None,
            "depth": 1,
        })
        time.sleep(0.05)
        ctx.events.emit(
            ctx.run_id,
            "tool_call_start",
            {
                "ts": _utcnow_iso(),
                "agent_name": "dummy_agent",
                "tool_name": "read_text_segment",
                "input": {"path": "input/doc_d1.txt", "start": 0, "length": 200},
                "invocation_id": parent_inv_id,
                "parent_invocation_id": None,
                "parent_agent_name": None,
                "depth": 1,
            },
        )
        time.sleep(0.05)
        ctx.events.emit(
            ctx.run_id,
            "tool_call_result",
            {
                "ts": _utcnow_iso(),
                "agent_name": "dummy_agent",
                "tool_name": "read_text_segment",
                "output_preview": "（dummy output）",
                "invocation_id": parent_inv_id,
                "parent_invocation_id": None,
                "parent_agent_name": None,
                "depth": 1,
            },
        )
        time.sleep(0.05)

        # 子エージェント開始（親エージェントの中から呼ばれる）
        ctx.events.emit(ctx.run_id, "agent_start", {
            "ts": _utcnow_iso(),
            "agent_name": "dummy_subagent",
            "is_subagent": True,
            "invocation_id": child_inv_id,
            "parent_invocation_id": parent_inv_id,
            "parent_agent_name": "dummy_agent",
            "depth": 2,
        })
        time.sleep(0.05)
        ctx.events.emit(
            ctx.run_id,
            "tool_call_start",
            {
                "ts": _utcnow_iso(),
                "agent_name": "dummy_subagent",
                "tool_name": "extract_regex_matches",
                "input": {"pattern": ".*", "path": "work/template_draft.md"},
                "invocation_id": child_inv_id,
                "parent_invocation_id": parent_inv_id,
                "parent_agent_name": "dummy_agent",
                "depth": 2,
            },
        )
        time.sleep(0.05)
        ctx.events.emit(
            ctx.run_id,
            "tool_call_error",
            {
                "ts": _utcnow_iso(),
                "agent_name": "dummy_subagent",
                "tool_name": "extract_regex_matches",
                "error": "dummy error",
                "invocation_id": child_inv_id,
                "parent_invocation_id": parent_inv_id,
                "parent_agent_name": "dummy_agent",
                "depth": 2,
            },
        )
        time.sleep(0.05)
        # 子エージェント終了
        ctx.events.emit(ctx.run_id, "agent_end", {
            "ts": _utcnow_iso(),
            "agent_name": "dummy_subagent",
            "is_subagent": True,
            "final_response": "（dummy subagent final response）",
            "final_response_preview": "（dummy subagent final response）",
            "invocation_id": child_inv_id,
            "parent_invocation_id": parent_inv_id,
            "parent_agent_name": "dummy_agent",
            "depth": 2,
        })
        time.sleep(0.05)
        # 親エージェント終了
        ctx.events.emit(ctx.run_id, "agent_end", {
            "ts": _utcnow_iso(),
            "agent_name": "dummy_agent",
            "is_subagent": False,
            "final_response": "（dummy agent final response）",
            "final_response_preview": "（dummy agent final response）",
            "invocation_id": parent_inv_id,
            "parent_invocation_id": None,
            "parent_agent_name": None,
            "depth": 1,
        })

