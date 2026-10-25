"""Validate the actual Windows ZIP from a new directory with no developer PATH."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    if sys.platform != "win32":
        raise SystemExit("This check currently targets the Windows portable artifact.")
    from pdf2md import __version__
    archive = ROOT / f"dist/PDF2MD-win32-AMD64-{__version__}.zip"
    expected = archive.with_suffix(".zip.sha256").read_text("utf-8").split()[0]
    with archive.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    assert actual == expected, "Archive checksum mismatch"
    target = ROOT / f"output/package-check-{__version__}"
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as package:
        for name in package.namelist():
            assert (target / name).resolve().is_relative_to(target.resolve())
        if "--reuse-extraction" not in sys.argv:
            package.extractall(target)  # Reading each member also validates ZIP CRC.
    app = target / "PDF2MD"
    executable = app / "PDF2MD.exe"
    environment = dict(os.environ)
    environment["WIN_PD_OVERRIDE_LOCAL_APPDATA"] = str(target / "profile")
    for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "PDF2MD_MODEL_DIR", "RAPID_MODELS_DIR"):
        environment.pop(name, None)
    windows = environment.get("SystemRoot", r"C:\Windows")
    environment["PATH"] = ";".join(
        str(Path(windows) / item)
        for item in (
            "System32",
            "",
            "System32/Wbem",
            "System32/WindowsPowerShell/v1.0",
            "System32/OpenSSH",
        )
    )

    def run(*args):
        subprocess.run(
            [str(executable), *map(str, args)],
            cwd=app,
            env=environment,
            check=True,
            timeout=240,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    probe_path = target / "desktop.json"
    run("--desktop-smoke", probe_path)
    probe = json.loads(probe_path.read_text("utf-8"))
    assert probe["ok"] and Path(probe["runtime"]).is_relative_to(app)
    results = []
    for fixture, key, minimum in (
        ("scanned-table.pdf", "table_count", 3),
        ("scanned-math.pdf", "formula_count", 1),
    ):
        output = target / Path(fixture).stem
        run("--convert", ROOT / "tests/fixtures" / fixture, "--output", output)
        report_path = next(output.glob("*/conversion-report.json"))
        report = json.loads(report_path.read_text("utf-8"))
        assert report["status"].startswith("succeeded") and report[key] >= minimum
        assert report["ocr_pages"] == 1
        markdown_path = report_path.parent / report["output"]["markdown"]
        markdown = markdown_path.read_text("utf-8")
        for resource in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", markdown):
            assert (markdown_path.parent / resource).is_file(), resource
        content_path = markdown_path.parent / "content_list.json"
        content = json.loads(content_path.read_text("utf-8"))
        injected = False
        if not content["warnings"]:
            # A clean recognition result still needs to exercise the review
            # state transition. This explicit test prompt uses real converter
            # regions/ranges and is not claimed as OCR quality evidence.
            block = next(b for b in content["blocks"]
                         if b.get("markdown_ranges") and b.get("source_regions"))
            content["warnings"] = [{
                "id": "portable-review-probe", "code": "low_ocr_confidence",
                "page": block["source_regions"][0]["page"],
                "message": "分发包核对流程验证用提示（人工添加）",
                "block_ids": [block["id"]], "markdown_ranges": block["markdown_ranges"],
                "source_regions": block["source_regions"],
            }]
            content_path.write_text(json.dumps(content, ensure_ascii=False), "utf-8")
            injected = True
        review_report = target / (Path(fixture).stem + "-review.json")
        run("--review-smoke", markdown_path, review_report)
        review = json.loads(review_report.read_text("utf-8"))
        assert review["ok"] and review["restart_restored"]
        results.append(
            {
                "fixture": fixture,
                "status": report["status"],
                "elapsed_seconds": report["elapsed_seconds"],
                "images": report["image_count"],
                "tables": report["table_count"],
                "formulas": report["formula_count"],
                "review": review,
                "review_prompt_injected": injected,
            }
        )
    record = {
        "ok": True,
        "archive": archive.name,
        "archive_bytes": archive.stat().st_size,
        "sha256": actual,
        "desktop": probe,
        "cases": results,
        "environment": "No developer PATH, PYTHONPATH, VIRTUAL_ENV or model-directory override",
    }
    destination = ROOT / f"output/qa/package-check-{__version__}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(record, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps(record, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
