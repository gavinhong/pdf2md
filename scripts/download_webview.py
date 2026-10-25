"""Download Microsoft's unmodified Fixed Version WebView2 for offline Windows packages."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]


def main():
    if sys.platform != "win32":
        print("macOS uses the system WKWebView; no separate browser runtime is needed.")
        return
    target = ROOT / "runtime" / "WebView2"
    if (target / "msedgewebview2.exe").is_file():
        print(f"Fixed WebView2 already present: {target}")
        return
    response = requests.get(
        "https://developer.microsoft.com/microsoft-edge/api/webview2", timeout=60
    )
    response.raise_for_status()
    release = response.json()[0]
    build = next(item for item in release["builds"] if item["architecture"] == "x64")
    cache = ROOT / ".cache" / "webview2"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / Path(build["url"]).name
    if not archive.exists():
        print(f"Downloading WebView2 {release['version']} x64", flush=True)
        with requests.get(build["url"], stream=True, timeout=(30, 120)) as download:
            download.raise_for_status()
            temporary = archive.with_suffix(".part")
            with temporary.open("wb") as output:
                for data in download.iter_content(4 * 1024 * 1024):
                    output.write(data)
            temporary.rename(archive)
    extracted = cache / "extracted"
    extracted.mkdir(exist_ok=True)
    subprocess.run(
        ["expand.exe", "-F:*", str(archive), str(extracted)], check=True, stdout=subprocess.DEVNULL
    )
    executable = next(extracted.rglob("msedgewebview2.exe"))
    shutil.copytree(executable.parent, target, dirs_exist_ok=True)
    record = {
        "version": release["version"],
        "architecture": "x64",
        "source": build["url"],
        "archive_sha256": hashlib.file_digest(archive.open("rb"), "sha256").hexdigest(),
    }
    (target.parent / "webview2-manifest.json").write_text(json.dumps(record, indent=2), "utf-8")
    print(f"Fixed WebView2 saved: {target}", flush=True)


if __name__ == "__main__":
    main()
