from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .paths import model_root

_verified: dict[tuple, dict] = {}


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def check_models(root: Path | None = None, full: bool = False) -> dict:
    root = (root or model_root()).resolve()
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"本地模型尚未准备好，请运行 scripts/download_models.py。模型目录：{root}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    required = {
        "layout",
        "formula",
        "table_unet",
        "table_slanet",
        "table_paddle_cls",
        "table_q_cls",
        "ocr_det",
        "ocr_rec",
        "ocr_dictionary",
        "orientation",
    }
    records = {record["id"]: record for record in manifest["files"]}
    if not required.issubset(records):
        raise ValueError("模型清单不完整，请重新准备模型目录。")
    paths = {}
    for identifier, record in records.items():
        path = (root / record["path"]).resolve()
        if (
            not path.is_relative_to(root)
            or not path.is_file()
            or path.stat().st_size != record["bytes"]
        ):
            raise ValueError(f"模型缺失或大小不符：{record['path']}，请重新准备模型。")
        key = (str(path), path.stat().st_mtime_ns, path.stat().st_size, record["sha256"])
        if full and key not in _verified:
            if digest(path) != record["sha256"]:
                raise ValueError(f"模型校验失败：{record['path']}，请重新下载该文件。")
            _verified[key] = record
        paths[identifier] = str(path)
    return {"root": str(root), "manifest": manifest, "paths": paths}


def model_status(root: Path | None = None) -> dict:
    try:
        info = check_models(root)
        return {
            "ready": True,
            "directory": info["root"],
            "count": len(info["paths"]),
            "bytes": info["manifest"]["total_bytes"],
            "error": "",
        }
    except (OSError, ValueError, KeyError) as exc:
        return {
            "ready": False,
            "directory": str(root or model_root()),
            "count": 0,
            "bytes": 0,
            "error": str(exc),
        }
