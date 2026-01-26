from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncGenerator, Callable, Optional, Set

from fastapi import Request
from fastapi.responses import StreamingResponse

from document_process_app.contracts import EventSink


def stream_run_events(
    request: Request,
    events: EventSink,
    run_id: str,
    *,
    allowed_types: Optional[Set[str]] = None,
    sanitize: Optional[Callable[[str, dict[str, Any]], dict[str, Any]]] = None,
    include_step_context: bool = False,
) -> StreamingResponse:
    last_id_hdr = request.headers.get("Last-Event-ID")
    try:
        last_id = int(last_id_hdr) if last_id_hdr else 0
    except Exception:
        last_id = 0

    async def gen() -> AsyncGenerator[bytes, None]:
        nonlocal last_id
        current_step: Optional[str] = None
        yield b": connected\n\n"
        while True:
            if await request.is_disconnected():
                break

            new_events = events.list(run_id, after_event_id=(last_id or None), limit=200)
            if not new_events:
                yield b": keepalive\n\n"
                await asyncio.sleep(0.5)
                continue

            for ev in new_events:
                last_id = ev.event_id
                if allowed_types is not None and ev.event_type not in allowed_types:
                    continue
                raw_payload = ev.payload or {}

                # Keep track of latest step context (stateful, in-order)
                if include_step_context and str(ev.event_type) in {"step_started", "step_waiting_user"}:
                    try:
                        step_name = str((raw_payload or {}).get("step") or "").strip()
                    except Exception:
                        step_name = ""
                    if step_name:
                        current_step = step_name

                payload = dict(raw_payload) if isinstance(raw_payload, dict) else {"payload": raw_payload}
                if include_step_context and current_step:
                    payload["_step_ctx"] = current_step
                if sanitize is not None:
                    try:
                        payload = sanitize(ev.event_type, payload) or {}
                    except Exception:
                        payload = {}
                data = json.dumps(
                    {
                        "event_id": ev.event_id,
                        "ts": ev.ts.isoformat(),
                        "event_type": ev.event_type,
                        "payload": payload,
                    },
                    ensure_ascii=False,
                )
                msg = f"id: {ev.event_id}\nevent: {ev.event_type}\ndata: {data}\n\n"
                yield msg.encode("utf-8")

    return StreamingResponse(gen(), media_type="text/event-stream")
