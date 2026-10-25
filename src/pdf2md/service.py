from __future__ import annotations

import json
import math
import multiprocessing as mp
import os
import threading
import time
import uuid
from pathlib import Path

import psutil

from .domain import PDFIUM_LOCK, Settings, Task, inspect_pdf, parse_pages, timestamp
from .models import model_status
from .output import clean_staging, json_write, publish
from .paths import config_file, model_root
from .review import atomic_write, file_digest, json_bytes, load_document, read_json, save_document
from .worker import worker_main


class ApplicationService:
    def __init__(self, settings_path: Path | None = None, models_path: Path | None = None,
                 deferred: bool = False):
        self.tasks: list[Task] = []
        self.settings_path = settings_path or config_file()
        self.models_path = models_path or model_root()
        self.settings = Settings()
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.io_lock = threading.RLock()
        self.worker_lock = threading.RLock()
        self.busy = False
        self.validating = False
        self.current = ""
        self.cancelled: set[str] = set()
        self.abort_all = False
        self.process = None
        self.connection = None
        self.worker_generation = 0
        self.revision = 0
        self.batch_thread = None
        self.init_thread = None
        self.warmup_thread = None
        self.warmup_epoch = 0
        self.warmup_generation = None
        self.closed = False
        self.deferred = deferred
        self.startup = {"state": "pending", "phase": "等待界面显示", "error": ""}
        self.warmup = {"state": "pending", "phase": "等待基础初始化", "profile": None, "error": ""}
        self.models = {"ready": False, "error": "", "missing": []}
        self.index_path = self.settings_path.with_name("tasks.json")
        self.source_checks = {}
        if not deferred:
            self._initialize_state()

    def _load_initial_state(self):
        # Read and validate into local values. Neither snapshots nor shutdown
        # should wait behind large historical Markdown files being hashed.
        settings = Settings()
        try:
            saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
            saved["page_range"] = ""
            settings = Settings.model_validate(saved)
        except (OSError, ValueError, TypeError):
            pass
        tasks = []
        try:
            index = json.loads(self.index_path.read_text(encoding="utf-8"))
            if not isinstance(index, dict):
                raise ValueError("任务索引格式无效，请恢复 tasks.json 后重试。")
        except FileNotFoundError:
            index = {}
        indexed_tasks = index.get("tasks")
        for item in (indexed_tasks if isinstance(indexed_tasks, list) else [])[:100]:
            if self.closed:
                break
            try:
                task = Task.model_validate(item)
                if task.status in ("running", "queued"):
                    task.status, task.phase = "cancelled", "上次运行已中断，可重试"
                if task.status.startswith("succeeded") and Path(task.markdown_file).is_file():
                    self._review_status(task, load_document(task))
                tasks.append(task)
            except (ValueError, OSError, TypeError, KeyError):
                continue
        with self.lock:
            if not self.closed:
                self.startup["phase"] = "正在检查本地模型"
                self.revision += 1
        return settings, tasks, model_status(self.models_path)

    def _initialize_state(self):
        try:
            settings, tasks, models = self._load_initial_state()
            with self.condition:
                if self.closed:
                    return
                self.settings, self.tasks, self.models = settings, tasks, models
                self.startup = {"state": "ready", "phase": "基础初始化完成", "error": ""}
                self.revision += 1
                self.condition.notify_all()
            if self.deferred:
                self._request_warmup(settings)
        except Exception as exc:
            with self.condition:
                if not self.closed:
                    self.startup = {"state": "failed", "phase": "初始化失败", "error": str(exc)}
                    self.revision += 1
                    self.condition.notify_all()

    def initialize(self):
        with self.lock:
            if self.closed:
                return {"ok": False, "error": "程序正在关闭。"}
            if self.startup["state"] in ("loading", "ready"):
                return {"ok": True}
            self.startup = {"state": "loading", "phase": "正在恢复历史任务", "error": ""}
            self.revision += 1
            self.init_thread = threading.Thread(target=self._initialize_state, daemon=True)
            self.init_thread.start()
        return {"ok": True}

    def _not_ready(self):
        if self.closed:
            return {"ok": False, "error": "程序正在关闭。"}
        if self.startup["state"] != "ready":
            return {"ok": False, "error": "正在恢复设置和历史任务，请稍候再操作。"}
        return None

    def _persist_tasks(self):
        with self.lock:
            # An early close must leave the existing index byte-for-byte intact.
            if self.startup["state"] != "ready":
                return
            atomic_write(self.index_path, json_bytes({
                "schema_version": "1.0", "tasks": [t.model_dump() for t in self.tasks]
            }))

    def _request_warmup(self, settings):
        from .warming import warmup_profile

        with self.condition:
            if self.closed or self.startup["state"] != "ready":
                return
            if self.warmup["state"] == "loading":
                return
            if self.warmup["state"] == "ready" and self.warmup["profile"] == warmup_profile(settings):
                return
            if self.warmup["state"] == "failed":
                return  # Keep the queue until an explicit retry.
            self.warmup = {"state": "loading", "phase": "正在启动识别进程",
                           "profile": warmup_profile(settings), "error": ""}
            self.revision += 1
            self.warmup_epoch += 1
            self.warmup_thread = threading.Thread(
                target=self._warmup_run, args=(settings.model_copy(), self.warmup_epoch), daemon=True)
            self.warmup_thread.start()

    def _warmup_run(self, settings, epoch):
        try:
            profile = self._perform_warmup(settings)
            with self.condition:
                if self.closed or epoch != self.warmup_epoch:
                    return
                self.warmup = {"state": "ready", "phase": "识别资源已就绪",
                               "profile": profile, "error": ""}
                self.warmup_generation = self.worker_generation if self.process else None
                self.revision += 1
                self.condition.notify_all()
        except Exception as exc:
            with self.lock:
                if self.closed or epoch != self.warmup_epoch:
                    return
            self._stop_worker()
            with self.condition:
                if self.closed or epoch != self.warmup_epoch:
                    return
                self.warmup.update(state="failed", phase="识别资源准备失败", error=str(exc))
                self.revision += 1
                self.condition.notify_all()

    def _perform_warmup(self, settings):
        models = model_status(self.models_path)
        with self.lock:
            self.models = models
            self.revision += 1
        with self.io_lock:
            if not self.models["ready"]:
                raise ValueError(self.models["error"])
            if self.closed:
                raise InterruptedError("程序正在关闭")
            self._ensure_worker()
            connection, process = self.connection, self.process
            generation, request_id = self.worker_generation, uuid.uuid4().hex
            connection.send_bytes(json.dumps({"type": "warmup", "request_id": request_id,
                "generation": generation, "settings": settings.model_dump()}).encode("utf-8"))
            while not self.closed:
                if connection.poll(0.15):
                    message = json.loads(connection.recv_bytes(4 * 1024 * 1024))
                    if (message.get("request_id"), message.get("generation")) != (request_id, generation):
                        continue
                    if message["type"] == "phase":
                        with self.lock:
                            self.warmup["phase"] = message["phase"]
                            self.revision += 1
                    elif message["type"] == "warmup_completed":
                        return message["profile"]
                    elif message["type"] == "warmup_failed":
                        raise RuntimeError(message["error"])
                elif not process.is_alive():
                    raise RuntimeError("识别进程意外退出，请重试。")
            raise InterruptedError("程序正在关闭")

    def retry_warmup(self):
        with self.lock:
            if error := self._not_ready():
                return error
            if self.warmup["state"] == "loading":
                return {"ok": True}
            if self.current and self._task(self.current).status == "running":
                return {"ok": False, "error": "请等待当前转换结束。"}
            # Claim the restart before releasing the state lock. A simultaneous
            # bridge/HTTP retry cannot stop the newly-created worker a second time.
            self.warmup_epoch += 1
            epoch, settings = self.warmup_epoch, self.settings.model_copy()
            self.warmup.update(state="loading", phase="正在重启识别进程", error="")
            self.revision += 1
        self._stop_worker()
        with self.lock:
            if not self.closed:
                self.warmup_thread = threading.Thread(
                    target=self._warmup_run, args=(settings, epoch), daemon=True)
                self.warmup_thread.start()
        return {"ok": True}

    def _wait_resources(self, settings, task_id):
        from .warming import warmup_profile

        if not self.deferred:
            return True  # Command-line conversions prepare synchronously in run.
        while True:
            with self.condition:
                if self.closed or self.abort_all or task_id in self.cancelled:
                    return False
                self._check_resource_process()
                if self.warmup["state"] == "ready" and self.warmup["profile"] == warmup_profile(settings):
                    return True
                self._request_warmup(settings)
                self.condition.wait(timeout=0.15)

    def _check_resource_process(self):
        # The green resource badge belongs to a living process, not just a
        # configuration. An idle crash must prepare the replacement before run.
        if (self.deferred and self.warmup["state"] == "ready" and
            self.warmup_generation is not None and
            (self.process is None or not self.process.is_alive() or
             self.warmup_generation != self.worker_generation)):
            self.warmup.update(state="pending", phase="正在重新准备识别资源", error="")
            self.revision += 1
            self._request_warmup(self.settings)

    @staticmethod
    def _review_status(task, document):
        task.warnings = document["warnings"]
        task.reviewed_count = document["reviewed_count"]
        task.pending_count = document["pending_count"]
        task.status = ("succeeded_with_warnings" if task.pending_count else
                       "succeeded_reviewed" if task.warnings else "succeeded")
        task.phase = ("转换完成，部分内容需核对" if task.pending_count else
                      "全部提示已核对" if task.warnings else "转换完成")

    def snapshot(self) -> dict:
        with self.lock:
            if not self.closed:
                self._check_resource_process()
            return {
                "revision": self.revision,
                "tasks": [task.model_dump() for task in self.tasks],
                "settings": self.settings.model_dump(),
                "busy": self.busy,
                "validating": self.validating,
                "current": self.current,
                "models": self.models,
                "startup": dict(self.startup),
                "warmup": dict(self.warmup),
            }

    def import_files(self, paths: list[str]) -> dict:
        with self.lock:
            if error := self._not_ready():
                return error
            if self.busy or self.validating:
                return {"ok": False, "error": "正在处理任务，请完成后再添加文件。"}
            self.validating = True
            self.revision += 1
        errors, added, duplicates = [], 0, 0
        try:
            for raw in paths:
                try:
                    path, size, pages = inspect_pdf(raw)
                    with self.lock:
                        if any(
                            os.path.normcase(task.source) == os.path.normcase(str(path))
                            for task in self.tasks
                        ):
                            duplicates += 1
                            continue
                        if len(self.tasks) >= 100:
                            raise ValueError("任务列表最多 100 个文件，请先移除已完成任务。")
                        self.tasks.append(
                            Task(
                                id=uuid.uuid4().hex,
                                source=str(path),
                                name=path.name,
                                size=size,
                                pages=pages,
                            )
                        )
                        added += 1
                        self.revision += 1
                except (OSError, ValueError) as exc:
                    errors.append({"name": Path(raw).name, "message": str(exc)})
        finally:
            with self.lock:
                self.validating = False
                self.revision += 1
            self._persist_tasks()
        return {"ok": True, "added": added, "duplicates": duplicates, "errors": errors}

    def remove(self, task_id: str = "") -> dict:
        with self.lock:
            if error := self._not_ready():
                return error
            if self.busy or self.validating:
                return {"ok": False, "error": "转换期间无法修改任务列表。"}
            self.tasks = [task for task in self.tasks if task.id != task_id] if task_id else []
            self.revision += 1
            self._persist_tasks()
        return {"ok": True}

    def retry(self, task_id: str) -> dict:
        with self.lock:
            if error := self._not_ready():
                return error
            if self.busy:
                return {"ok": False, "error": "请等待本批任务结束后重试。"}
            task = self._task(task_id)
            if task.status not in ("failed", "cancelled"):
                return {"ok": False, "error": "该任务无需重试。"}
            task.status, task.error, task.phase = "draft", "", "待开始"
            task.completed_pages = 0
            self.revision += 1
            self._persist_tasks()
        return {"ok": True}

    def start(self, payload: dict) -> dict:
        with self.lock:
            if error := self._not_ready():
                return error
            if self.busy or self.validating:
                return {"ok": False, "error": "已有任务正在处理。"}
            try:
                settings = Settings.model_validate(payload)
                if not settings.output_dir:
                    raise ValueError("请先选择输出目录。")
                destination = Path(settings.output_dir).expanduser().resolve()
                destination.mkdir(parents=True, exist_ok=True)
                probe = destination / f".pdf2md-write-{uuid.uuid4().hex}"
                probe.write_bytes(b"")
                probe.unlink()
                settings.output_dir = str(destination)
                self.models = model_status(self.models_path)
                if not self.models["ready"]:
                    raise ValueError(self.models["error"])
            except (OSError, ValueError) as exc:
                return {"ok": False, "error": str(exc)}
            candidates = [task for task in self.tasks if task.status == "draft"]
            if not candidates:
                return {"ok": False, "error": "没有待转换的文件；失败任务可先点击重试。"}
            selected = []
            for task in candidates:
                try:
                    pages = parse_pages(settings.page_range, task.pages)
                    task.status, task.phase, task.error = "queued", "等待识别资源" if self.deferred else "等待中", ""
                    task.selected_pages = len(pages)
                    selected.append((task.id, pages))
                except ValueError as exc:
                    task.status, task.error, task.phase = "failed", str(exc), "页码范围有误"
            self.settings = settings
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            self.settings_path.write_text(settings.model_dump_json(indent=2), encoding="utf-8")
            self.cancelled.clear()
            self.abort_all = False
            self.busy = bool(selected)
            self.revision += 1
            self._persist_tasks()
            if selected:
                self.batch_thread = threading.Thread(
                    target=self._batch, args=(selected, settings), daemon=True
                )
                self.batch_thread.start()
            return {"ok": True, "queued": len(selected)}

    def _task(self, task_id: str) -> Task:
        return next(task for task in self.tasks if task.id == task_id)

    def _ensure_worker(self) -> None:
        with self.worker_lock:
            if self.closed:
                raise InterruptedError("程序正在关闭")
            if self.process is not None and self.process.is_alive():
                return
            self._stop_worker()
            parent, child = mp.get_context("spawn").Pipe()
            self.connection = parent
            self.process = mp.get_context("spawn").Process(
                target=worker_main, args=(child, str(self.models_path)), daemon=True
            )
            self.worker_generation += 1
            self.process.start()
            child.close()

    def _stop_worker(self) -> None:
        # A single lock owns process teardown. Closing is allowed to interrupt
        # the IO owner's poll instead of waiting for model loading to finish.
        with self.worker_lock:
            process, connection = self.process, self.connection
            self.process = self.connection = None
            self.worker_generation += 1
            if process is not None:
                try:
                    if process.is_alive():
                        parent = psutil.Process(process.pid)
                        children = parent.children(recursive=True)
                        for child in children:
                            try:
                                child.terminate()
                            except psutil.Error:
                                pass
                        process.terminate()
                        process.join(timeout=3)
                        if process.is_alive():
                            process.kill()
                            process.join(timeout=2)
                        for child in children:
                            try:
                                if child.is_running():
                                    child.kill()
                            except psutil.Error:
                                pass
                    else:
                        process.join(timeout=0.1)
                except (OSError, psutil.Error):
                    pass
            if connection is not None:
                connection.close()
        if self.deferred and not self.closed and self.warmup["state"] == "ready":
            with self.condition:
                self.warmup.update(state="pending", phase="正在重新准备识别资源", error="")
                self.revision += 1
                self.condition.notify_all()

    def _batch(self, jobs: list[tuple[str, list[int]]], settings: Settings) -> None:
        output_root = Path(settings.output_dir)
        try:
            for task_id, pages in jobs:
                staging = None
                owns_io = False
                with self.lock:
                    task = self._task(task_id)
                    if self.abort_all or task_id in self.cancelled or self.closed:
                        task.status, task.phase = "cancelled", "已取消"
                        self.revision += 1
                        continue
                    self.current = task_id
                    task.phase = "等待识别资源" if self.deferred else "启动本地转换进程"
                    self.revision += 1
                started = None
                try:
                    if not self._wait_resources(settings, task_id):
                        raise InterruptedError("任务已取消")
                    started = time.monotonic()
                    with self.lock:
                        task.status, task.phase = "running", "启动本地转换进程"
                        task.started_at = timestamp()
                        task.completed_pages = 0
                    # Warmup and conversion serialize the entire request/reply
                    # exchange. There is never a second Pipe receiver.
                    self.io_lock.acquire()
                    owns_io = True
                    staging = output_root / ".pdf2md-tmp" / uuid.uuid4().hex
                    staging.mkdir(parents=True)
                    self._ensure_worker()
                    connection, process = self.connection, self.process
                    generation, request_id = self.worker_generation, uuid.uuid4().hex
                    connection.send_bytes(
                        json.dumps(
                            {
                                "type": "run", "request_id": request_id,
                                "generation": generation,
                                "task_id": task_id,
                                "source": task.source,
                                "pages": pages,
                                "settings": settings.model_dump(),
                                "staging": str(staging),
                            }
                        ).encode("utf-8")
                    )
                    while True:
                        with self.lock:
                            cancelled = task_id in self.cancelled or self.abort_all
                        if cancelled:
                            self._stop_worker()
                            raise InterruptedError("任务已取消")
                        if connection.poll(0.15):
                            message = json.loads(connection.recv_bytes(4 * 1024 * 1024))
                            if (message.get("task_id") != task_id or
                                (message.get("request_id"), message.get("generation")) !=
                                    (request_id, generation)):
                                continue
                            kind = message["type"]
                            with self.lock:
                                if kind == "phase":
                                    task.phase = message["phase"]
                                elif kind == "progress":
                                    task.completed_pages = message["completed_pages"]
                                self.revision += 1
                            if kind == "failed":
                                raise ValueError(message["error"])
                            if kind == "completed":
                                report = message["report"]
                                with self.lock:
                                    report.update(
                                        task_id=task_id,
                                        source_total_pages=task.pages,
                                        status="succeeded_with_warnings"
                                        if report["warnings"]
                                        else "succeeded",
                                    )
                                    report["settings"]["output_dir"] = "."
                                    json_write(staging / "conversion-report.json", report)
                                    final = publish(
                                        staging,
                                        output_root,
                                        Path(task.name).stem,
                                        lambda: task_id in self.cancelled or self.abort_all,
                                    )
                                    task.output_dir = str(final)
                                    task.markdown_file = str(final / f"{final.name}.md")
                                    task.warnings = report["warnings"]
                                    task.pending_count = len(task.warnings)
                                    task.reviewed_count = 0
                                    task.source_sha256 = report.get("source_sha256", "")
                                    content_path = final / "content_list.json"
                                    content = read_json(content_path)
                                    content.setdefault("source", {})["path"] = task.source
                                    atomic_write(content_path, json_bytes(content))
                                    task.status = (
                                        "succeeded_with_warnings" if task.warnings else "succeeded"
                                    )
                                    task.phase = (
                                        "转换完成，部分内容需核对" if task.warnings else "转换完成"
                                    )
                                    task.completed_pages = len(pages)
                                break
                        elif not process.is_alive():
                            raise RuntimeError("转换进程意外退出，请缩小页面范围后重试。")
                except InterruptedError:
                    with self.lock:
                        task.status, task.phase = "cancelled", "已取消"
                except Exception as exc:
                    self._stop_worker()
                    with self.lock:
                        task.status, task.error, task.phase = "failed", str(exc), "转换失败"
                finally:
                    if owns_io:
                        self.io_lock.release()
                    clean_staging(staging, output_root)
                    with self.lock:
                        task.elapsed = round(time.monotonic() - started, 1) if started else 0
                        task.finished_at = timestamp()
                        self.current = ""
                        self.revision += 1
                        self._persist_tasks()
        finally:
            with self.lock:
                self.busy = False
                self.current = ""
                self.revision += 1
            if self.deferred and not self.closed and self.warmup["state"] == "pending":
                self._request_warmup(self.settings)

    def cancel(self, all_tasks: bool = False) -> dict:
        with self.lock:
            if all_tasks:
                self.abort_all = True
            target = self.current or next((task.id for task in self.tasks
                                           if task.status == "queued"), "")
            if target:
                self.cancelled.add(target)
                self._task(target).phase = "正在取消"
            self.revision += 1
            self.condition.notify_all()
        return {"ok": True}

    def preview(self, task_id: str, offset: int = 0, limit: int = 100_000) -> dict:
        with self.lock:
            task = self._task(task_id)
            path = Path(task.markdown_file)
        if not path.is_file():
            return {"ok": False, "error": "结果文件不存在，可能已移动或删除。请重新转换。"}
        text = path.read_text(encoding="utf-8")
        end = min(len(text), offset + min(limit, 500_000))
        if end < len(text):
            # Keep a whole block (including HTML tables and math) intact.
            next_break = text.find("\n\n", end)
            if next_break >= 0:
                end = next_break
            else:
                end = len(text)
        return {
            "ok": True,
            "markdown": text[offset:end],
            "next_offset": end,
            "has_more": end < len(text),
            "characters": len(text),
        }

    def _source_status(self, task, metadata):
        path = Path(task.source) if task.source else None
        expected = metadata.get("sha256") or task.source_sha256
        result = {"available": False, "changed": False, "pages": task.pages}
        if path is None or not path.is_file():
            return result
        result["available"] = True
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        actual = self.source_checks.get(key)
        if actual is None:
            actual = file_digest(path)
            self.source_checks = {key: actual, **dict(list(self.source_checks.items())[-20:])}
        if expected and actual != expected:
            result["changed"] = True
        else:
            if not task.source_sha256:
                task.source_sha256 = actual
        return result

    def review_document(self, task_id: str) -> dict:
        try:
            with self.lock:
                if error := self._not_ready():
                    return error
                task = self._task(task_id)
                if not task.status.startswith("succeeded"):
                    raise ValueError("请等待转换完成后核对。")
                result = load_document(task)
                task.pages = max(task.pages, max((p.get("page", 0)
                                                for p in result["pages"]), default=0))
                result["source"] = self._source_status(task, result.pop("source_metadata"))
                self._review_status(task, result)
                self.revision += 1
                self._persist_tasks()
                return result
        except (OSError, ValueError, StopIteration, UnicodeError) as exc:
            return {"ok": False, "error": str(exc) or "任务不存在。"}

    def save_review(self, task_id: str, payload: dict) -> dict:
        try:
            with self.lock:
                if error := self._not_ready():
                    return error
                task = self._task(task_id)
                if not task.status.startswith("succeeded"):
                    return {"ok": False, "error": "任务尚未完成，无法保存核对。"}
                result = save_document(task, payload)
                if result["ok"]:
                    result["source"] = self._source_status(task, result.pop("source_metadata"))
                    self._review_status(task, result)
                    self.revision += 1
                    self._persist_tasks()
                elif result.get("markdown_saved"):
                    # A partial two-file commit must never leave an old green badge
                    # attached to new, unreviewed Markdown.
                    self._review_status(task, load_document(task))
                    self.revision += 1
                    self._persist_tasks()
                return result
        except (OSError, ValueError, StopIteration) as exc:
            return {"ok": False, "error": str(exc) or "任务不存在。"}

    def review_page(self, task_id: str, page: int, scale: float = 1.5) -> dict:
        import pypdfium2 as pdfium

        try:
            if isinstance(page, bool) or not isinstance(page, int):
                raise ValueError("请填写有效页码。")
            scale = float(scale)
            if not math.isfinite(scale) or not 0.25 <= scale <= 4:
                raise ValueError("缩放比例须在 25%–400% 之间。")
            with self.lock:
                task = self._task(task_id)
                metadata = read_json(Path(task.output_dir) / "content_list.json").get("source", {})
                status = self._source_status(task, metadata)
                if not status["available"] or status["changed"]:
                    raise ValueError("原 PDF 已变化，请重新转换。" if status["changed"] else
                                     "原 PDF 不存在，请重新关联源文件。")
                source = Path(task.source)
                target_dir = Path(task.output_dir) / ".review-pages"
                target_dir.mkdir(exist_ok=True)
                # Serialize every PDFium access in the UI/service process, including imports.
                with PDFIUM_LOCK, pdfium.PdfDocument(source) as pdf:
                    if not 1 <= page <= len(pdf):
                        raise ValueError(f"页码超出范围，请填写 1–{len(pdf)}。")
                    pdf_page = pdf[page - 1]
                    try:
                        width, height = pdf_page.get_size()
                        effective = min(scale, (16_000_000 / max(width * height, 1)) ** 0.5)
                        effective = min(effective, 6000 / max(width, height))
                        token = task.source_sha256[:12]
                        filename = f"{token}-{page}-{effective:.4f}.png"
                        target = target_dir / filename
                        if not target.is_file():
                            bitmap = pdf_page.render(scale=effective)
                            try:
                                image = bitmap.to_pil()
                                try:
                                    image.save(target, "PNG")
                                finally:
                                    image.close()
                            finally:
                                bitmap.close()
                        from PIL import Image
                        with Image.open(target) as rendered:
                            size = rendered.size
                    finally:
                        pdf_page.close()
                # Keep only the most recently used page bitmaps for this document.
                target.touch()
                cached = sorted(target_dir.glob("*.png"), key=lambda p: p.stat().st_mtime,
                                reverse=True)
                for old in cached[8:]:
                    try:
                        old.unlink()
                    except OSError:
                        pass
                return {"ok": True, "resource": f".review-pages/{filename}",
                        "width": size[0], "height": size[1], "page": page}
        except (OSError, ValueError, StopIteration, pdfium.PdfiumError) as exc:
            return {"ok": False, "error": str(exc) or "任务不存在。"}

    def open_review_result_path(self, md_path: str, source_path: str = "") -> dict:
        with self.lock:
            if error := self._not_ready():
                return error
        try:
            path = Path(md_path).expanduser().resolve(strict=True)
            if path.suffix.lower() != ".md" or not path.is_file():
                raise ValueError("请选择转换结果的 Markdown 文件。")
            report = read_json(path.parent / "conversion-report.json")
            content = read_json(path.parent / "content_list.json")
            if not report and not content:
                raise ValueError("该目录没有 PDF2MD 转换记录，请选择转换结果目录中的 Markdown。")
            declared = report.get("output", {}).get("markdown")
            if (declared and path.name != declared) or path.name.endswith(".original.md"):
                raise ValueError("请选择该目录的正式转换结果，原始备份不能作为独立核对任务。")
            with self.lock:
                for task in self.tasks:
                    if task.markdown_file and Path(task.markdown_file).resolve() == path:
                        return {"ok": True, "task_id": task.id}
                if len(self.tasks) >= 100:
                    raise ValueError("任务列表最多 100 项，请先移除已完成任务。")
                metadata = content.get("source", {})
                source = source_path or metadata.get("path", "")
                source_name = metadata.get("name") or report.get("source_name") or path.stem + ".pdf"
                if not source:
                    for candidate in (path.parent / source_name, path.parent.parent / source_name,
                                      Path.cwd() / source_name):
                        if candidate.is_file():
                            source = str(candidate.resolve())
                            break
                task = Task(id=uuid.uuid4().hex, source=source, name=source_name,
                            pages=report.get("source_total_pages") or
                            max(report.get("selected_pages") or metadata.get("selected_pages")
                                or [p.get("page", 0) for p in content.get("pages", [])] or [0]),
                            status="succeeded", output_dir=str(path.parent), markdown_file=str(path),
                            source_sha256=metadata.get("sha256", ""))
                if source and Path(source).is_file():
                    _, task.size, task.pages = inspect_pdf(source)
                document = load_document(task)
                self._review_status(task, document)
                self.tasks.append(task)
                self.revision += 1
                self._persist_tasks()
            return {"ok": True, "task_id": task.id}
        except (OSError, ValueError, TypeError) as exc:
            return {"ok": False, "error": str(exc)}

    def locate_review_source_path(self, task_id: str, path: str) -> dict:
        with self.lock:
            if error := self._not_ready():
                return error
        try:
            source, size, pages = inspect_pdf(path)
            sha = file_digest(source)
            with self.lock:
                task = self._task(task_id)
                metadata = read_json(Path(task.output_dir) / "content_list.json").get("source", {})
                expected = metadata.get("sha256") or task.source_sha256
                if expected and expected != sha:
                    raise ValueError("所选 PDF 与原转换文件不同，请选择原文件或重新转换。")
                task.source, task.size, task.pages = str(source), size, pages
                task.source_sha256 = sha
                self._persist_tasks()
                self.revision += 1
            return self.review_document(task_id)
        except (OSError, ValueError, StopIteration) as exc:
            return {"ok": False, "error": str(exc) or "任务不存在。"}

    def export_report(self, destination: str) -> dict:
        tasks = self.snapshot()["tasks"]
        for task in tasks:
            for path_key in ("source", "markdown_file", "output_dir"):
                if task.get(path_key):
                    task["error"] = task["error"].replace(
                        task[path_key], task["name"] if path_key == "source" else "[本地输出目录]"
                    )
            task.pop("source", None)
            task.pop("markdown_file", None)
            task.pop("output_dir", None)
        json_write(
            Path(destination),
            {"schema_version": "1.0", "generated_at": timestamp(), "tasks": tasks},
        )
        return {"ok": True}

    def shutdown(self) -> None:
        with self.condition:
            self.closed = True
            self.abort_all = True
            self.condition.notify_all()
        self.cancel(all_tasks=True)
        self._stop_worker()
        for thread in (self.init_thread, self.warmup_thread, self.batch_thread):
            if thread and thread is not threading.current_thread():
                thread.join(timeout=3)
        self._persist_tasks()
