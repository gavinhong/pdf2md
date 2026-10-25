from __future__ import annotations

import json
import sys
import threading
import traceback
from pathlib import Path


def worker_main(connection, model_directory: str) -> None:
    if sys.stdout is None or sys.stderr is None:
        from .paths import cache_root

        log = (cache_root() / "worker.log").open("w", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log
    from .domain import Settings
    from .engine import RapidDocAdapter, restrict_network

    sender_lock = threading.Lock()
    task_id = ""
    request_id = ""
    generation = 0

    def emit(message: dict) -> None:
        with sender_lock:
            connection.send_bytes(
                json.dumps({**message, "task_id": task_id, "request_id": request_id,
                            "generation": generation}, ensure_ascii=False).encode("utf-8")
            )

    adapter = None
    restrict_network()
    try:
        while True:
            message = json.loads(connection.recv_bytes(1024 * 1024))
            if message["type"] == "shutdown":
                break
            kind = message.get("type")
            if kind not in ("run", "warmup"):
                continue
            task_id = message.get("task_id", "") if kind == "run" else ""
            request_id = message.get("request_id", "")
            generation = message.get("generation", 0)
            try:
                settings = Settings.model_validate(message["settings"])
                if adapter is None:
                    emit({"type": "phase", "phase": "校验本地模型"})
                    adapter = RapidDocAdapter(Path(model_directory), emit)
                if kind == "warmup":
                    profile = adapter.warmup(settings)
                    emit({"type": "warmup_completed", "profile": profile})
                    continue
                report = adapter.convert(
                    Path(message["source"]),
                    message["pages"],
                    settings,
                    Path(message["staging"]),
                )
                emit({"type": "completed", "report": report})
            except Exception as exc:
                # Never put PDF body or model output in diagnostics.
                print(traceback.format_exc(), flush=True)
                emit({"type": "warmup_failed" if kind == "warmup" else "failed",
                      "error": str(exc)[:1500] or "识别引擎发生异常，请重试。"})
    except (EOFError, BrokenPipeError, OSError):
        pass
    finally:
        connection.close()
