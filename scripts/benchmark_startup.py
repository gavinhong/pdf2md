"""Measure an owned Windows desktop process without changing user settings.

Requires the workspace's existing Node/playwright-core installation. FCP comes
from Chromium's retained PaintTiming entry, mapped to process launch via its
epoch timeOrigin. Core/model readiness are observed snapshots (upper bounds),
not invented application timestamps. The first run is first in this sequence;
this tool does not flush Windows' filesystem/standby cache or claim a cold boot.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import shutil
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

OBSERVER = r"""
import { chromium } from 'playwright-core';
const port = Number(process.env.PDF2MD_BENCH_PORT);
const deadline = Date.now() + Number(process.env.PDF2MD_BENCH_TIMEOUT) * 1000;
const conversion = process.env.PDF2MD_BENCH_CONVERT ? JSON.parse(process.env.PDF2MD_BENCH_CONVERT) : null;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
process.stdout.write(JSON.stringify({observer_ready:true}) + '\n');
let browser;
try {
  while (Date.now() < deadline) {
    try { browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`, {timeout:1000}); break; }
    catch { await sleep(50); }
  }
  if (!browser) throw new Error('CDP endpoint did not become available');
  let page;
  while (Date.now() < deadline) {
    page = browser.contexts().flatMap(context => context.pages()).find(p => /^http:\/\/127\.0\.0\.1:\d+\//.test(p.url()));
    if (page) break;
    await sleep(25);
  }
  if (!page) throw new Error('PDF2MD page did not become available');
  await page.waitForFunction(() => window.__PDF2MD_TOKEN__ && window.pdf2mdAction &&
    document.querySelector('#add-button'), null, {timeout:Math.max(1000,deadline-Date.now())});
  const sampled = await page.evaluate(async ({deadline,conversion}) => {
    const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
    const stages = [];
    let coreAt = null, modelsAt = null, last = '', state = null;
    const firstSnapshotEntry = performance.getEntriesByType('resource').find(e => /\/api\/snapshot$/.test(e.name));
    while (Date.now() < deadline) {
      const response = await fetch('/api/snapshot', {method:'POST', headers:{'Content-Type':'application/json',
        'X-PDF2MD-Token':window.__PDF2MD_TOKEN__},body:JSON.stringify({args:[]})});
      if (!response.ok) throw new Error(`snapshot returned ${response.status}`);
      state = await response.json();
      const now = performance.timeOrigin + performance.now();
      const startup = state.startup;
      const warmup = state.warmup;
      const phase = [startup?.state,startup?.phase,warmup?.state,warmup?.phase].join('|');
      if (phase !== last) { stages.push({epoch_ms:now,startup:startup??null,warmup:warmup??null}); last=phase; }
      if (startup?.state === 'failed') throw new Error(startup.error || 'Core initialization failed');
      if ((!startup || startup.state === 'ready') && !coreAt && !document.querySelector('#add-button').disabled) coreAt = now;
      if (warmup?.state === 'failed') throw new Error(warmup.error || 'Model warmup failed');
      if (coreAt && (!warmup || warmup.state === 'ready')) {
        if (warmup) modelsAt = now;
        break;
      }
      await sleep(50);
    }
    if (!coreAt) throw new Error('Core readiness timeout');
    if (state.warmup && !modelsAt) throw new Error('Model readiness timeout');
    // Core callbacks can run before the renderer presents the first frame.
    // Allow PaintTiming delivery without moving the already-recorded core time.
    const paintDeadline = Date.now() + 2500;
    let fcp;
    while (Date.now() < paintDeadline) {
      fcp = performance.getEntriesByType('paint').find(p => p.name === 'first-contentful-paint');
      if (fcp) break;
      await sleep(50);
    }
    let converted = null;
    if (conversion) {
      const api = async (method,args) => {
        const response = await fetch(`/api/${method}`,{method:'POST',headers:{'Content-Type':'application/json',
          'X-PDF2MD-Token':window.__PDF2MD_TOKEN__},body:JSON.stringify({args})});
        if (!response.ok) throw new Error(`${method} returned ${response.status}`);
        return response.json();
      };
      const cleared = await api('remove',[]);
      if (!cleared.ok) throw new Error(cleared.error || 'Could not clear isolated benchmark queue');
      const imported = await api('import_files',[[conversion.pdf]]);
      if (!imported.ok || imported.added !== 1) throw new Error(JSON.stringify(imported));
      const click = performance.timeOrigin + performance.now();
      const started = await api('start',[{output_dir:conversion.output,page_range:conversion.pages,mode:conversion.mode}]);
      if (!started.ok || started.queued !== 1) throw new Error(JSON.stringify(started));
      while (Date.now() < deadline) {
        const snapshot = await api('snapshot',[]);
        const task = snapshot.tasks[0];
        if (task && (task.status.startsWith('succeeded') || ['failed','cancelled'].includes(task.status))) {
          const complete = performance.timeOrigin + performance.now();
          converted = {scenario:state.warmup ? 'prewarmed_first_conversion' : 'cold_process_first_conversion',
            pdf:conversion.pdf,pages:conversion.pages,mode:conversion.mode,task_id:task.id,status:task.status,
            ok:task.status.startsWith('succeeded'),error:task.error||'',click_epoch_ms:click,
            complete_observed_epoch_ms:complete,click_to_complete_observed_ms:complete-click,
            markdown_file:task.markdown_file,output_dir:task.output_dir};
          break;
        }
        await sleep(100);
      }
      if (!converted) throw new Error('First conversion timeout');
    }
    return {time_origin_epoch_ms:performance.timeOrigin,
      fcp_epoch_ms:fcp ? performance.timeOrigin+fcp.startTime : null,
      fcp_navigation_ms:fcp?.startTime??null,core_observed_epoch_ms:coreAt,
      models_observed_epoch_ms:modelsAt, stages,
      first_snapshot_response_epoch_ms:firstSnapshotEntry ? performance.timeOrigin+firstSnapshotEntry.responseEnd : null,
      paint_timing_supported:PerformanceObserver.supportedEntryTypes.includes('paint'),
      paint_entries:performance.getEntriesByType('paint').map(p => ({name:p.name,startTime:p.startTime})),
      visibility_state:document.visibilityState,document_has_focus:document.hasFocus(),
      conversion:converted,
      navigation:performance.getEntriesByType('navigation').map(n => ({
        name:n.name,startTime:n.startTime,fetchStart:n.fetchStart,requestStart:n.requestStart,
        responseStart:n.responseStart,responseEnd:n.responseEnd,domInteractive:n.domInteractive,
        domContentLoadedEventStart:n.domContentLoadedEventStart,domContentLoadedEventEnd:n.domContentLoadedEventEnd,
        loadEventStart:n.loadEventStart,loadEventEnd:n.loadEventEnd,transferSize:n.transferSize,
        encodedBodySize:n.encodedBodySize,decodedBodySize:n.decodedBodySize})),
      resources:performance.getEntriesByType('resource').map(r => ({
        name:r.name.replace(/\/resource\/[^/]+\//,'/resource/[SESSION]/'),
        startTime:r.startTime,responseEnd:r.responseEnd,duration:r.duration,
        initiatorType:r.initiatorType,transferSize:r.transferSize,
        encodedBodySize:r.encodedBodySize,decodedBodySize:r.decodedBodySize,
        renderBlockingStatus:r.renderBlockingStatus??null})),
      tasks:state.tasks.length,models_available:state.models.ready,
      startup_state:state.startup?.state??null,warmup_state:state.warmup?.state??'not_available',
      _origin:location.origin,_token:window.__PDF2MD_TOKEN__};
  }, {deadline,conversion});
  const metricsSession = await page.context().newCDPSession(page);
  await metricsSession.send('Performance.enable');
  const { metrics } = await metricsSession.send('Performance.getMetrics');
  sampled.cdp_metrics = Object.fromEntries(metrics.filter(m =>
    ['NavigationStart','FirstMeaningfulPaint','DomContentLoaded','TaskDuration'].includes(m.name))
    .map(m => [m.name,m.value]));
  process.stdout.write(JSON.stringify({ok:true,...sampled})+'\n', () => process.exit(0));
} catch (error) {
  process.stdout.write(JSON.stringify({ok:false,error:String(error)})+'\n', () => process.exit(1));
}
"""


def available_port() -> int:
    with socket.socket() as bound:
        bound.bind(("127.0.0.1", 0))
        return bound.getsockname()[1]


class Windows:
    def __init__(self):
        self.api = ctypes.WinDLL("user32", use_last_error=True)
        self.callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.api.EnumWindows.argtypes = [self.callback_type, wintypes.LPARAM]
        self.api.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.api.IsWindowVisible.argtypes = [wintypes.HWND]
        self.api.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.api.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.api.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.api.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self.api.SetForegroundWindow.argtypes = [wintypes.HWND]
        self.api.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                         ctypes.c_int, ctypes.c_int, wintypes.UINT]
        self.api.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        self.api.GetWindowLongW.restype = wintypes.LONG

    def visible(self, pid: int) -> list[dict]:
        results = []

        def inspect(hwnd, _context):
            owner = wintypes.DWORD()
            self.api.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value != pid or not self.api.IsWindowVisible(hwnd):
                return True
            title = ctypes.create_unicode_buffer(1024)
            self.api.GetWindowTextW(hwnd, title, len(title))
            rect = wintypes.RECT()
            self.api.GetWindowRect(hwnd, ctypes.byref(rect))
            if "PDF2MD" in title.value and rect.right - rect.left > 100 and rect.bottom - rect.top > 100:
                results.append({"hwnd": int(hwnd), "title": title.value, "pid": owner.value})
            return True

        self.api.EnumWindows(self.callback_type(inspect), 0)
        return results

    def close(self, hwnd: int) -> None:
        self.api.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE, only this owned process's HWND.

    def show_front(self, hwnd: int) -> bool:
        self.api.ShowWindow(hwnd, 9)  # SW_RESTORE, only this owned process's HWND.
        return bool(self.api.SetForegroundWindow(hwnd))

    def topmost(self, hwnd: int) -> dict:
        accepted = bool(self.api.SetWindowPos(
            hwnd, wintypes.HWND(-1), 0, 0, 0, 0, 0x0002 | 0x0001 | 0x0010  # NOMOVE|NOSIZE|NOACTIVATE
        ))
        verified = bool(self.api.GetWindowLongW(hwnd, -20) & 0x00000008)  # WS_EX_TOPMOST
        return {"topmost_request_accepted": accepted, "topmost_verified": verified}


def write_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def create_history_seed(directory: Path, source_pdf: Path, task_count: int = 100,
                        document_bytes: int = 16 * 1024 * 1024) -> dict:
    """Prepare a clearly synthetic large-history scenario, not a PDF fixture.

    One Markdown file is hardlinked into independently named result folders;
    the two app versions must receive identical copies of this task index.
    This function is intentionally separate from normal benchmarking so seed
    creation can finish before any timed process/model work starts.
    """
    if not 1 <= task_count <= 100 or not 1024 <= document_bytes <= 32 * 1024 * 1024:
        raise ValueError("History seed must stay within 100 tasks and 32 MiB per Markdown.")
    directory = directory.resolve()
    source_pdf = source_pdf.resolve(strict=True)
    if source_pdf.suffix.lower() != ".pdf" or not source_pdf.is_file():
        raise ValueError("History tasks must reference an existing PDF, not a generated dummy PDF.")
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / "history-seed.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text("utf-8"))
        expected = (task_count, document_bytes, str(source_pdf))
        actual = (manifest.get("task_count"), manifest.get("document_bytes"), manifest.get("source_pdf"))
        if actual != expected:
            raise ValueError("The existing history seed uses different inputs; choose a new directory.")
        return manifest
    base = directory / "synthetic-history-base.md"
    if not base.exists():
        paragraph = (b"Synthetic history stress benchmark paragraph. "
                     b"This is generated Markdown for startup measurements only.\n\n")
        prefix = b"# Synthetic large-history startup benchmark\n\n"
        with base.open("xb") as stream:
            stream.write(prefix)
            remaining = document_bytes - len(prefix) - 2
            chunk = paragraph * 512
            while remaining:
                part = chunk[:remaining]
                stream.write(part)
                remaining -= len(part)
            stream.write(b"\n\n")
    if base.stat().st_size != document_bytes:
        raise ValueError("The existing seed Markdown has the wrong size; choose a new directory.")
    tasks = []
    for index in range(1, task_count + 1):
        result_dir = directory / "results" / f"history-{index:03d}"
        result_dir.mkdir(parents=True, exist_ok=True)
        markdown = result_dir / "history.md"
        if not markdown.exists():
            os.link(base, markdown)
        if not os.path.samefile(base, markdown):
            raise ValueError("The history scenario requires hardlinks to the one shared seed file.")
        content = {"schema_version": "1.0", "source": {"name": source_pdf.name,
                   "selected_pages": [1]}, "pages": [{"page": 1, "size": [595, 842]}],
                   "blocks": [], "warnings": []}
        report = {"schema_version": "1.0", "source_name": source_pdf.name,
                  "selected_pages": [1], "source_total_pages": 1, "warnings": [],
                  "status": "succeeded", "output": {"markdown": markdown.name}}
        write_record(result_dir / "content_list.json", content)
        write_record(result_dir / "conversion-report.json", report)
        tasks.append({"id": f"synthetic-history-{index:03d}", "source": str(source_pdf),
                      "name": source_pdf.name, "size": source_pdf.stat().st_size, "pages": 1,
                      "status": "succeeded", "phase": "转换完成", "completed_pages": 1,
                      "selected_pages": 1, "warnings": [], "output_dir": str(result_dir),
                      "markdown_file": str(markdown)})
    write_record(directory / "tasks.json", {"schema_version": "1.0", "tasks": tasks})
    write_record(directory / "settings.json", {
        "output_dir": "", "page_range": "", "mode": "auto", "extract_images": True,
        "table_format": "auto", "remove_headers": True, "formula": True,
    })
    manifest = {"schema_version": "1.0", "synthetic": True,
                "scenario": "Large-history stress case, not ordinary small-document startup",
                "task_count": task_count, "document_bytes": document_bytes,
                "logical_markdown_bytes": document_bytes * task_count,
                "physical_markdown_bytes": document_bytes, "source_pdf": str(source_pdf),
                "base_markdown": str(base)}
    write_record(manifest_path, manifest)
    return manifest


def install_history_seed(seed: Path, profile: Path) -> dict:
    """Copy the exact same index/settings to each isolated benchmark profile."""
    seed = seed.resolve(strict=True)
    manifest = json.loads((seed / "history-seed.json").read_text("utf-8"))
    if not manifest.get("synthetic"):
        raise ValueError("The supplied history seed is not marked as synthetic.")
    config = profile / "PDF2MD"
    config.mkdir(parents=True, exist_ok=True)
    for name in ("settings.json", "tasks.json"):
        shutil.copyfile(seed / name, config / name)
    return manifest


def one_run(command: list[str], application_cwd: Path, node: str, profile: Path,
            timeout: float, index: int, conversion: dict | None = None, topmost: bool = False) -> dict:
    import psutil

    port = available_port()
    environment = dict(os.environ)
    # platformdirs uses KnownFolder APIs on Windows, not APPDATA/LOCALAPPDATA.
    environment["WIN_PD_OVERRIDE_LOCAL_APPDATA"] = str(profile)
    environment["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
        f"--remote-debugging-port={port} --disable-background-timer-throttling "
        "--disable-renderer-backgrounding --disable-backgrounding-occluded-windows"
    )
    environment["PDF2MD_BENCH_PORT"] = str(port)
    environment["PDF2MD_BENCH_TIMEOUT"] = str(timeout)
    if conversion:
        environment["PDF2MD_BENCH_CONVERT"] = json.dumps(conversion, ensure_ascii=False)
    else:
        environment.pop("PDF2MD_BENCH_CONVERT", None)
    for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "PDF2MD_MODEL_DIR"):
        environment.pop(name, None)
    observer = subprocess.Popen(
        [node, "--input-type=module", "-e", OBSERVER], cwd=ROOT, env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert observer.stdout is not None
    ready = json.loads(observer.stdout.readline())
    if not ready.get("observer_ready"):
        raise RuntimeError("The CDP observer did not initialize")
    win = Windows()
    visible = {}
    stop_polling = threading.Event()
    available_mb = round(psutil.virtual_memory().available / 1024**2, 1)
    launch_epoch_ms, launched = time.time() * 1000, time.perf_counter()
    application = subprocess.Popen(command, cwd=application_cwd, env=environment,
                                   creationflags=subprocess.CREATE_NO_WINDOW)

    def poll_window():
        while not stop_polling.is_set() and application.poll() is None:
            windows = win.visible(application.pid)
            if windows:
                visible.update(windows[0], elapsed_ms=(time.perf_counter() - launched) * 1000)
                visible["foreground_request_accepted"] = win.show_front(windows[0]["hwnd"])
                if topmost:
                    visible.update(win.topmost(windows[0]["hwnd"]))
                return
            stop_polling.wait(0.01)

    monitor = threading.Thread(target=poll_window, daemon=True)
    monitor.start()
    measurements, forced = {}, False
    try:
        remaining, stderr = observer.communicate(timeout=timeout + 10)
        lines = [line for line in remaining.splitlines() if line.strip()]
        measurements = json.loads(lines[-1]) if lines else {"ok": False, "error": stderr[-2000:]}
        if not measurements.get("ok"):
            raise RuntimeError(measurements.get("error", "CDP measurements failed"))
        monitor.join(timeout=1)
        if not visible:
            raise RuntimeError("The owned native window was not observed visible")
        isolation = (profile / "PDF2MD").is_dir()
        if not isolation:
            raise RuntimeError("platformdirs profile override did not take effect")
        result = {
            "index": index,
            "sequence": "first_in_sequence" if index == 1 else "subsequent",
            "pid": application.pid, "visible_window": {k: v for k, v in visible.items() if k != "elapsed_ms"},
            "launch_epoch_ms": launch_epoch_ms, "available_ram_mb_at_launch": available_mb,
            "visible_ms": round(visible["elapsed_ms"], 3),
            "fcp_ms": round(measurements["fcp_epoch_ms"] - launch_epoch_ms, 3)
            if measurements.get("fcp_epoch_ms") is not None else None,
            "core_observed_ms": round(measurements["core_observed_epoch_ms"] - launch_epoch_ms, 3),
            "models_observed_ms": round(measurements["models_observed_epoch_ms"] - launch_epoch_ms, 3)
            if measurements.get("models_observed_epoch_ms") is not None else None,
            "cdp": {key: value for key, value in measurements.items() if not key.startswith("_")},
            "isolated_profile_confirmed": isolation,
        }
        if measurements.get("conversion"):
            result["conversion"] = measurements["conversion"]
            result["conversion"]["process_to_complete_observed_ms"] = round(
                measurements["conversion"]["complete_observed_epoch_ms"] - launch_epoch_ms, 3
            )
        return result
    finally:
        stop_polling.set()
        monitor.join(timeout=1)
        if application.poll() is None:
            for window in win.visible(application.pid):
                win.close(window["hwnd"])
            try:
                application.wait(timeout=12)
            except subprocess.TimeoutExpired:
                # A failed close handler must not leave benchmark processes behind.
                # This authenticated fallback only targets the just-launched app.
                if measurements.get("_origin"):
                    request = urllib.request.Request(
                        measurements["_origin"] + "/api/finish_close", data=b'{"args":[]}',
                        headers={"Content-Type": "application/json",
                                 "X-PDF2MD-Token": measurements["_token"]},
                    )
                    try:
                        with urllib.request.urlopen(request, timeout=10):
                            pass
                    except OSError:
                        pass
                try:
                    application.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    forced = True
                    application.terminate()  # Only the Popen process owned by this run.
                    application.wait(timeout=5)
        if observer.poll() is None:
            observer.terminate()
            observer.wait(timeout=5)
        if "result" in locals():
            result["exit_code"] = application.returncode
            result["forced_shutdown"] = forced


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    application = parser.add_mutually_exclusive_group(required=True)
    application.add_argument("--exe", type=Path, help="Packaged desktop executable")
    application.add_argument("--source", type=Path, nargs="?", const=ROOT / "run.py",
                             help="Source launcher; defaults to this workspace's run.py")
    parser.add_argument("--label", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--runs", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--node", default=shutil.which("node"))
    parser.add_argument("--convert", type=Path, help="Optional first conversion after startup/model readiness")
    parser.add_argument("--output-dir", type=Path, help="Parent for per-run conversion outputs")
    parser.add_argument("--pages", default="1", help="Conversion page range; default: 1")
    parser.add_argument("--mode", choices=("auto", "ocr"), default="ocr")
    parser.add_argument("--topmost", action="store_true",
                        help="Keep only the owned benchmark HWND above other windows without activating it")
    parser.add_argument("--history-seed", type=Path,
                        help="Prepared synthetic history directory; copies identical settings/tasks to the profile")
    args = parser.parse_args()
    if sys.platform != "win32":
        raise SystemExit("This benchmark uses Windows native HWND visibility and WebView2 CDP.")
    if not args.node or not 1 <= args.runs <= 20:
        raise SystemExit("An existing Node installation and 1–20 runs are required.")
    if args.convert and not args.output_dir:
        raise SystemExit("--convert requires --output-dir; benchmark outputs never overwrite the source.")
    conversion_pdf = args.convert.resolve(strict=True) if args.convert else None
    launcher = (args.exe or args.source).resolve(strict=True)
    command = [str(launcher)] if args.exe else [sys.executable, str(launcher)]
    profile = (args.profile or args.output.parent / (args.label + "-profile")).resolve()
    from platformdirs import user_config_path
    if (profile / "PDF2MD").resolve() == user_config_path("PDF2MD", appauthor=False).resolve():
        raise SystemExit("Refusing to benchmark against the real user settings/history directory.")
    profile.mkdir(parents=True, exist_ok=True)
    history = install_history_seed(args.history_seed, profile) if args.history_seed else None
    record = {"schema_version": "1.0", "label": args.label, "command": command,
              "mode": "frozen" if args.exe else "source",
              "history_scenario": history,
              "profile": str(profile), "platform": platform.platform(), "cpu_count": os.cpu_count(),
              "sampling": {"native_visibility_poll_ms": 10, "readiness_poll_ms": 50,
                           "window": "Owned native window restored and requested foreground at first visibility",
                           "owned_window_topmost": args.topmost,
                           "browser_flags": "disable-background-timer-throttling, disable-renderer-backgrounding, disable-backgrounding-occluded-windows",
                           "first_run": "First in this sequence; OS cache was not flushed",
                           "fcp": "Retained Chromium PaintTiming mapped using epoch timeOrigin",
                           "readiness": "Observed snapshot upper bounds, includes CDP attachment delay",
                           "conversion": ("First isolated task; start API request to terminal snapshot, 100ms polling; "
                                          "process-to-complete also includes initialization/model preparation"
                                          if args.convert else "Not measured; no PDF import or inference requested")},
              "runs": []}
    try:
        for index in range(1, args.runs + 1):
            conversion = None
            if conversion_pdf:
                conversion = {"pdf": str(conversion_pdf), "pages": args.pages, "mode": args.mode,
                              "output": str((args.output_dir / f"{args.label}-run-{index}").resolve())}
            result = one_run(command, launcher.parent, args.node, profile, args.timeout,
                             index, conversion, args.topmost)
            record["runs"].append(result)
            write_record(args.output, record)
            print(json.dumps({"run": index, **{key: result[key] for key in
                  ("visible_ms", "fcp_ms", "core_observed_ms", "models_observed_ms", "exit_code")}}), flush=True)
            if result["forced_shutdown"]:
                raise RuntimeError("The owned application required forced shutdown; further runs stopped")
            if result.get("conversion") and not result["conversion"]["ok"]:
                raise RuntimeError("The first conversion failed: " + result["conversion"]["error"])
        record["median_ms"] = {key: statistics.median([run[key] for run in record["runs"] if run[key] is not None])
                               if any(run[key] is not None for run in record["runs"]) else None
                               for key in ("visible_ms", "fcp_ms", "core_observed_ms", "models_observed_ms")}
        record["ok"] = True
    except Exception as exc:
        record.update(ok=False, error=str(exc))
        raise
    finally:
        write_record(args.output, record)


if __name__ == "__main__":
    main()
