from __future__ import annotations

import json
import mimetypes
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING

from .paths import cache_root, resource_root, web_root

if TYPE_CHECKING:
    from .service import ApplicationService


def json_write(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class DeferredService:
    """The native window/static server need no document or model dependencies."""

    def __init__(self):
        self._lock = threading.RLock()
        self._instance = None
        self._thread = None
        self._closed = False
        self._startup = {"state": "pending", "phase": "等待界面显示", "error": ""}
        self._revision = 0

    @property
    def lock(self):
        return self._instance.lock if self._instance else self._lock

    def snapshot(self):
        with self._lock:
            if self._instance:
                return self._instance.snapshot()
            return {"revision": self._revision, "tasks": [], "settings": {},
                    "busy": False, "validating": False, "current": "",
                    "models": {"ready": False, "error": ""}, "startup": dict(self._startup),
                    "warmup": {"state": "pending", "phase": "等待基础初始化",
                               "profile": None, "error": ""}}

    def initialize(self):
        with self._lock:
            if self._closed:
                return {"ok": False, "error": "程序正在关闭。"}
            if self._instance:
                return self._instance.initialize()
            if self._startup["state"] == "loading":
                return {"ok": True}
            self._startup = {"state": "loading", "phase": "正在恢复历史任务", "error": ""}
            self._revision += 1
            self._thread = threading.Thread(target=self._create, daemon=True)
            self._thread.start()
        return {"ok": True}

    def _create(self):
        try:
            from .service import ApplicationService

            instance = ApplicationService(deferred=True)
            with self._lock:
                if self._closed:
                    instance.shutdown()
                    return
                # The instance is still pending, so no empty list can persist.
                instance.revision = self._revision
                self._instance = instance
                instance.initialize()
        except Exception as exc:
            with self._lock:
                self._startup = {"state": "failed", "phase": "初始化失败", "error": str(exc)}
                self._revision += 1

    def _task(self, task_id):
        if self._instance is None:
            raise StopIteration()
        return self._instance._task(task_id)

    def __getattr__(self, name):
        instance = self._instance
        if instance is not None:
            return getattr(instance, name)
        return lambda *args, **kwargs: {"ok": False, "error": "正在恢复设置和历史任务，请稍候再操作。"}

    def shutdown(self):
        with self._lock:
            self._closed = True
            instance = self._instance
        if instance:
            instance.shutdown()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)


class DesktopBridge:
    def __init__(self, service: ApplicationService):
        self._service = service
        self._window = None

    def snapshot(self):
        return self._service.snapshot()

    def initialize(self):
        return self._service.initialize()

    def retry_warmup(self):
        return self._service.retry_warmup()

    def import_files(self, paths):
        return self._service.import_files(paths)

    def choose_files(self):
        if self._window is None:
            return {"ok": False, "error": "请使用桌面窗口选择文件。"}
        import webview

        paths = self._window.create_file_dialog(
            webview.FileDialog.OPEN, allow_multiple=True, file_types=("PDF 文件 (*.pdf)",)
        )
        return self._service.import_files(list(paths)) if paths else {"ok": True, "added": 0}

    def choose_output(self):
        if self._window is None:
            return {"ok": False, "error": "请在输出目录输入框填写本地路径。"}
        import webview

        paths = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        return {"ok": True, "path": str(paths[0]) if paths else ""}

    def start(self, settings):
        return self._service.start(settings)

    def cancel(self, all_tasks=False):
        return self._service.cancel(all_tasks)

    def remove(self, task_id=""):
        return self._service.remove(task_id)

    def retry(self, task_id):
        return self._service.retry(task_id)

    def preview(self, task_id, offset=0):
        return self._service.preview(task_id, offset)

    def review_document(self, task_id):
        return self._service.review_document(task_id)

    def review_page(self, task_id, page, scale=1.5):
        reply = self._service.review_page(task_id, page, scale)
        if reply.get("ok"):
            reply["url"] = (f"/resource/{self._server_token}/{task_id}/" + reply.pop("resource"))
        return reply

    def save_review(self, task_id, payload):
        return self._service.save_review(task_id, payload)

    def open_review_result(self):
        if self._window is None:
            return {"ok": False, "error": "请在桌面窗口选择已有转换结果。"}
        import webview
        paths = self._window.create_file_dialog(
            webview.FileDialog.OPEN, allow_multiple=False,
            file_types=("Markdown 文件 (*.md)",)
        )
        return (self._service.open_review_result_path(str(paths[0])) if paths
                else {"ok": True, "cancelled": True})

    def locate_review_source(self, task_id):
        if self._window is None:
            return {"ok": False, "error": "请在桌面窗口选择原 PDF。"}
        import webview
        paths = self._window.create_file_dialog(
            webview.FileDialog.OPEN, allow_multiple=False, file_types=("PDF 文件 (*.pdf)",)
        )
        return (self._service.locate_review_source_path(task_id, str(paths[0])) if paths
                else {"ok": True, "cancelled": True})

    def open_result(self, task_id, folder=True):
        with self._service.lock:
            task = self._service._task(task_id)
            path = Path(task.output_dir if folder else task.markdown_file)
        if not path.exists():
            return {"ok": False, "error": "结果文件不存在，请重新转换。"}
        if sys.platform == "win32":
            os.startfile(path)
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])
        return {"ok": True}

    def open_external(self, url):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("https", "http") or not parsed.hostname:
            return {"ok": False, "error": "仅支持在系统浏览器中打开网页链接。"}
        return {"ok": bool(webbrowser.open(url))}

    def export_report(self):
        if self._window is None:
            return {"ok": False, "error": "请在桌面窗口导出任务报告。"}
        import webview

        chosen = self._window.create_file_dialog(
            webview.FileDialog.SAVE,
            save_filename="pdf2md-tasks.json",
            file_types=("JSON 报告 (*.json)",),
        )
        return self._service.export_report(str(chosen[0])) if chosen else {"ok": True}

    def finish_close(self):
        self._service.shutdown()
        if self._window:
            self._window._pdf2md_allow_close = True
            self._window.destroy()
        return {"ok": True}

    def invoke(self, name, args):
        allowed = {
            "snapshot",
            "initialize",
            "retry_warmup",
            "import_files",
            "choose_files",
            "choose_output",
            "start",
            "cancel",
            "remove",
            "retry",
            "preview",
            "review_document",
            "review_page",
            "save_review",
            "open_review_result",
            "locate_review_source",
            "open_result",
            "open_external",
            "export_report",
            "finish_close",
        }
        if name not in allowed:
            return {"ok": False, "error": "不支持的操作。"}
        try:
            return getattr(self, name)(*args)
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:1000]}


class LocalServer:
    def __init__(self, bridge: DesktopBridge, port: int = 0):
        self.bridge = bridge
        self.token = secrets.token_urlsafe(32)
        bridge._server_token = self.token
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, code, payload, content_type="application/json; charset=utf-8"):
                body = (
                    json.dumps(payload, ensure_ascii=False).encode("utf-8")
                    if not isinstance(payload, bytes)
                    else payload
                )
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            def do_GET(self):
                path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
                if path.startswith("/resource/"):
                    parts = path.split("/", 4)
                    if len(parts) != 5 or not secrets.compare_digest(parts[2], outer.token):
                        return self.send(403, {"error": "无权读取资源"})
                    task_id, relative = parts[3:]
                    try:
                        with bridge._service.lock:
                            task = bridge._service._task(task_id)
                            if not task.output_dir:
                                raise ValueError()
                            base = Path(task.output_dir).resolve()
                        target = (base / relative).resolve()
                        if not target.is_relative_to(base) or not target.is_file():
                            raise ValueError()
                        return self.send(
                            200,
                            target.read_bytes(),
                            mimetypes.guess_type(target.name)[0] or "application/octet-stream",
                        )
                    except (OSError, ValueError, StopIteration):
                        return self.send(404, {"error": "资源不存在"})
                base = web_root().resolve()
                target = (base / (path.lstrip("/") or "index.html")).resolve()
                if not target.is_relative_to(base) or not target.is_file():
                    return self.send(404, {"error": "文件不存在"})
                body = target.read_bytes()
                if target.name == "index.html":
                    body = body.replace(b"__SESSION_TOKEN__", outer.token.encode())
                return self.send(
                    200, body, mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                )

            def do_POST(self):
                if not secrets.compare_digest(self.headers.get("X-PDF2MD-Token", ""), outer.token):
                    return self.send(
                        403, {"ok": False, "error": "本地会话已失效，请重新打开程序。"}
                    )
                origin = self.headers.get("Origin")
                expected_origin = f"http://127.0.0.1:{outer.server.server_port}"
                if origin and origin != expected_origin:
                    return self.send(403, {"ok": False, "error": "请求来源不匹配。"})
                if not self.path.startswith("/api/"):
                    return self.send(404, {"ok": False})
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    return self.send(400, {"ok": False, "error": "请求长度无效。"})
                if not 0 <= length <= 256 * 1024 * 1024:
                    return self.send(413, {"ok": False, "error": "请求过大，未保存正文。"})
                try:
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    return self.send(200, bridge.invoke(self.path[5:], payload.get("args", [])))
                except (ValueError, OSError):
                    return self.send(400, {"ok": False, "error": "请求格式无效。"})

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def launch(web: bool = False, port: int = 0, smoke_report: Path | None = None):
    service = DeferredService()
    bridge = DesktopBridge(service)
    server = LocalServer(bridge, port)
    server.start()
    if web:
        print(server.url, flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        finally:
            service.shutdown()
            server.stop()
        return
    import webview

    runtime = resource_root() / "runtime" / "WebView2"
    if not runtime.is_dir():
        runtime = Path(sys.executable).parent / "runtime" / "WebView2"
    if runtime.is_dir():
        webview.settings["WEBVIEW2_RUNTIME_PATH"] = str(runtime)
    window = webview.create_window(
        "PDF2MD · PDF 转 Markdown",
        server.url,
        js_api=bridge,
        width=1380,
        height=880,
        min_size=(900, 620),
        text_select=True,
        hidden=bool(smoke_report),
    )
    bridge._window = window

    def closing():
        if not getattr(window, "_pdf2md_allow_close", False):
            # FormClosing runs on the native UI thread. run_js waits for a
            # renderer callback scheduled on that same thread, so dispatch it
            # elsewhere and return before asking JavaScript about dirty state.
            threading.Thread(
                target=window.run_js,
                args=("window.pdf2mdRequestClose && window.pdf2mdRequestClose()",),
                daemon=True,
            ).start()
            return False
        return True

    def close_probe():
        # Automated probes own no editable draft and must not route their
        # programmatic teardown through the interactive leave confirmation.
        window._pdf2md_allow_close = True
        window.destroy()

    def loaded():
        from webview.dom import DOMEventHandler

        def dropped(event):
            payload = event.get("dataTransfer", event.get("domTransfer", {}))
            files = payload.get("files", [])
            paths = [item.get("pywebviewFullPath", item.get("path", "")) for item in files]
            paths = [path for path in paths if path]
            if paths:
                reply = service.import_files(paths)
                window.run_js(
                    "window.pdf2mdImported(" + json.dumps(reply, ensure_ascii=False) + ")"
                )

        try:
            window.dom.document.events.drop += DOMEventHandler(dropped, prevent_default=True)
        except (AttributeError, TypeError):
            # The explicit native file picker is always available.
            pass
        if smoke_report:
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    ready = window.evaluate_js(
                        "Boolean(window.pywebview?.api?.snapshot && window.pdf2mdAction)"
                    )
                    if ready:
                        break
                    time.sleep(0.1)
                # Hidden verification windows may throttle animation frames;
                # initialize remains the same idempotent post-UI operation.
                service.initialize()
                while time.monotonic() < deadline:
                    state = service.snapshot()
                    if state["startup"]["state"] in ("ready", "failed"):
                        break
                    time.sleep(0.1)
                detail = window.evaluate_js(
                    "({title: document.title, body: document.body.innerText, token: window.__PDF2MD_TOKEN__.length})"
                )
                state = service.snapshot()
                ok = bool(ready and state["models"]["ready"] and
                          state["startup"]["state"] == "ready" and "转换队列" in detail["body"])
                json_write(
                    smoke_report,
                    {
                        "ok": ok,
                        "title": detail["title"],
                        "bridge_ready": bool(ready),
                        "runtime": str(runtime),
                        "models_ready": state["models"]["ready"],
                        "startup": state["startup"],
                    },
                )
            except Exception as exc:
                json_write(smoke_report, {"ok": False, "error": str(exc)})
            finally:
                close_probe()

    window.events.closing += closing
    window.events.loaded += loaded
    watchdog = threading.Timer(30, close_probe) if smoke_report else None
    if watchdog:
        watchdog.start()
    try:
        webview.start(
            gui="edgechromium" if sys.platform == "win32" else None,
            private_mode=True,
            storage_path=str(cache_root() / "webview"),
        )
    finally:
        if watchdog:
            watchdog.cancel()
        service.shutdown()
        server.stop()
