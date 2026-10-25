"""Build a native portable directory; optionally include Fixed WebView2 and zip it."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--with-webview", action="store_true")
    parser.add_argument("--zip", action="store_true")
    args = parser.parse_args()
    subprocess.run([sys.executable, "run.py", "--check-models"], cwd=ROOT, check=True)
    subprocess.run([sys.executable, "scripts/collect_licenses.py"], cwd=ROOT, check=True)
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "packaging/PDF2MD.spec"],
        cwd=ROOT,
        check=True,
    )
    target = ROOT / "dist" / "PDF2MD"
    # Models sit beside the executable on Windows; inside app resources on macOS.
    if sys.platform == "darwin":
        target = ROOT / "dist" / "PDF2MD.app" / "Contents" / "Resources"
    shutil.copytree(
        ROOT / "models",
        target / "models",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("*.part", "__pycache__"),
    )
    for name in ("README.md", "THIRD_PARTY_NOTICES.md", "REQUIREMENTS.md", "TECHNICAL_DESIGN.md"):
        shutil.copy2(ROOT / name, target / name)
    (target / "docs").mkdir(exist_ok=True)
    for name in ("verification.md", "startup.md"):
        shutil.copy2(ROOT / "docs" / name, target / "docs" / name)
    shutil.copytree(ROOT / "licenses", target / "licenses", dirs_exist_ok=True)
    if args.with_webview and sys.platform == "win32":
        subprocess.run([sys.executable, "scripts/download_webview.py"], cwd=ROOT, check=True)
        shutil.copytree(
            ROOT / "runtime" / "WebView2", target / "runtime" / "WebView2", dirs_exist_ok=True
        )
        shutil.copy2(
            ROOT / "runtime/webview2-manifest.json", target / "runtime/webview2-manifest.json"
        )
    records = [
        {"path": str(path.relative_to(target)).replace("\\", "/"), "bytes": path.stat().st_size}
        for path in target.rglob("*")
        if path.is_file()
    ]
    (target / "package-manifest.json").write_text(
        json.dumps(
            {
                "platform": sys.platform,
                "architecture": platform.machine(),
                "total_bytes": sum(item["bytes"] for item in records),
                "files": records,
            },
            ensure_ascii=False,
            indent=2,
        ),
        "utf-8",
    )
    print(
        f"Portable directory: {target}; {sum(item['bytes'] for item in records) / 1024**2:.1f} MiB",
        flush=True,
    )
    if args.zip:
        source = ROOT / "dist" / ("PDF2MD.app" if sys.platform == "darwin" else "PDF2MD")
        from pdf2md import __version__
        archive = ROOT / "dist" / f"PDF2MD-{sys.platform}-{platform.machine()}-{__version__}.zip"
        if sys.platform == "darwin":
            # Preserve .app framework symlinks and resource metadata.
            subprocess.run(
                ["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(source), str(archive)],
                check=True,
            )
        else:
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as output:
                for path in source.rglob("*"):
                    if path.is_file():
                        output.write(path, str(path.relative_to(source.parent)))
        with archive.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        archive.with_suffix(".zip.sha256").write_text(f"{checksum}  {archive.name}\n", "utf-8")
        print(f"Archive: {archive}; {archive.stat().st_size / 1024**2:.1f} MiB", flush=True)


if __name__ == "__main__":
    main()
