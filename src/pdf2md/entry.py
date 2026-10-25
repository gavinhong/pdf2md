from __future__ import annotations

import argparse
import multiprocessing
import sys


def main():
    multiprocessing.freeze_support()
    if sys.stdout is None or sys.stderr is None:
        from .paths import cache_root

        log = (cache_root() / "application.log").open("w", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log
    parser = argparse.ArgumentParser(description="PDF2MD · 本地 PDF 转 Markdown")
    parser.add_argument("--web", action="store_true", help="在本地浏览器中验证界面")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--check-models", action="store_true")
    parser.add_argument("--convert", metavar="PDF", help="使用同一转换引擎进行命令行转换")
    parser.add_argument("--output", default="output")
    parser.add_argument("--pages", default="")
    parser.add_argument("--ocr", action="store_true")
    parser.add_argument("--desktop-smoke", metavar="REPORT", help=argparse.SUPPRESS)
    parser.add_argument("--review-smoke", nargs=2, metavar=("MARKDOWN", "REPORT"),
                        help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.review_smoke:
        import json
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from . import __version__
        from .review import atomic_write, json_bytes
        from .service import ApplicationService

        markdown_path, report_path = map(Path, args.review_smoke)
        with TemporaryDirectory(prefix="pdf2md-review-smoke-") as temporary:
            settings_path = Path(temporary) / "settings.json"
            service = ApplicationService(settings_path=settings_path)
            try:
                opened = service.open_review_result_path(str(markdown_path))
                assert opened["ok"], opened
                task_id = opened["task_id"]
                document = service.review_document(task_id)
                assert document["ok"] and document["source"]["available"], document
                page = document["warnings"][0]["page"] if document["warnings"] else 1
                rendered = service.review_page(task_id, page, 1)
                assert rendered["ok"] and rendered["width"] > 0, rendered
                saved = service.save_review(task_id, {
                    "markdown": document["markdown"], "expected_sha256": document["sha256"],
                    "expected_revision": document["review_revision"], "changes": [],
                    "review_updates": {w["id"]: True for w in document["warnings"]},
                })
                assert saved["ok"] and saved["pending_count"] == 0, saved
                service.shutdown()
                restored = ApplicationService(settings_path=settings_path)
                try:
                    again = restored.review_document(task_id)
                    assert again["ok"] and again["pending_count"] == 0
                    atomic_write(report_path, json_bytes({
                        "ok": True, "version": __version__, "warning_count": len(saved["warnings"]),
                        "located_warnings": sum(bool(w["markdown_ranges"] and w["source_regions"])
                                                for w in saved["warnings"]),
                        "source_page_rendered": page, "page_size": [rendered["width"], rendered["height"]],
                        "status": restored.tasks[0].status, "restart_restored": True,
                    }))
                finally:
                    restored.shutdown()
            finally:
                service.shutdown()
        return
    if args.check_models:
        import json

        from .models import check_models

        info = check_models(full=True)
        print(
            json.dumps(
                {
                    "directory": info["root"],
                    "files": len(info["paths"]),
                    "bytes": info["manifest"]["total_bytes"],
                },
                ensure_ascii=False,
            )
        )
        return
    if args.convert:
        import time
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from .service import ApplicationService

        # CLI conversions own an isolated queue; desktop history must not add
        # unrelated drafts or make a previously converted source a duplicate.
        session = TemporaryDirectory(prefix="pdf2md-cli-")
        service = ApplicationService(settings_path=Path(session.name) / "settings.json")
        imported = service.import_files([args.convert])
        if not imported["added"]:
            print(imported, file=sys.stderr)
            raise SystemExit(1)
        response = service.start(
            {
                "output_dir": args.output,
                "page_range": args.pages,
                "mode": "ocr" if args.ocr else "auto",
            }
        )
        if not response["ok"]:
            print(response["error"], file=sys.stderr)
            raise SystemExit(1)
        previous = ""
        while service.busy:
            task = service.snapshot()["tasks"][0]
            if task["phase"] != previous:
                print(task["phase"], flush=True)
                previous = task["phase"]
            time.sleep(0.3)
        task = service.snapshot()["tasks"][0]
        service.shutdown()
        if task["status"].startswith("succeeded"):
            print(task["markdown_file"], flush=True)
        else:
            print(task["error"] or task["phase"], file=sys.stderr)
            raise SystemExit(1)
        return
    from .desktop import launch

    if args.desktop_smoke:
        import json
        from pathlib import Path

        from .output import json_write

        report = Path(args.desktop_smoke).resolve()
        report.parent.mkdir(parents=True, exist_ok=True)
        try:
            launch(smoke_report=report)
        except Exception as exc:
            json_write(report, {"ok": False, "error": str(exc)})
        if not report.exists() or not json.loads(report.read_text("utf-8"))["ok"]:
            raise SystemExit(1)
    else:
        launch(args.web, args.port)


if __name__ == "__main__":
    main()
