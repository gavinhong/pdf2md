"""Materialize the fixed RapidDoc CPU profile, including package assets, locally."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=ROOT / "models")
    args = parser.parse_args()
    target = args.directory.resolve()
    target.mkdir(parents=True, exist_ok=True)
    package = Path(importlib.util.find_spec("rapid_doc").origin).parent
    records: list[dict] = []
    remote = []
    definitions = [
        ("layout", "layout/rapid_layout_self/configs/default_models.yaml", "pp_doclayoutv3"),
        (
            "formula",
            "formula/rapid_formula_self/configs/default_models.yaml",
            "onnxruntime.pp_formulanet_plus_m",
        ),
        ("table_unet", "table/rapid_table_self/default_models.yaml", "unet"),
        ("table_slanet", "table/rapid_table_self/default_models.yaml", "slanet_plus"),
        ("table_paddle_cls", "table/rapid_table_self/default_models.yaml", "paddle_cls"),
        ("table_q_cls", "table/rapid_table_self/default_models.yaml", "q_cls"),
    ]
    for identifier, config, key in definitions:
        value = yaml.safe_load((package / "model" / config).read_text(encoding="utf-8"))
        for item in key.split("."):
            value = value[item]
        url = value["model_dir_or_path"]
        remote.append(
            {
                "id": identifier,
                "path": Path(url).name,
                "source": url,
                "sha256": value["SHA256"],
                "license": "See THIRD_PARTY_NOTICES.md",
            }
        )

    def download(record: dict) -> dict:
        path = target / record["path"]
        if path.is_file() and sha256(path) == record["sha256"]:
            print(f"cached {record['id']}", flush=True)
        else:
            temporary = path.with_suffix(path.suffix + ".part")
            for attempt in range(4):
                try:
                    print(f"download {record['id']} -> {path.name}", flush=True)
                    with requests.get(record["source"], stream=True, timeout=(30, 120)) as response:
                        response.raise_for_status()
                        with temporary.open("wb") as output:
                            for chunk in response.iter_content(1024 * 1024):
                                output.write(chunk)
                    if sha256(temporary) != record["sha256"]:
                        raise ValueError(f"SHA-256 mismatch: {record['id']}")
                    temporary.replace(path)
                    break
                except (requests.RequestException, OSError, ValueError):
                    if attempt == 3:
                        raise
                    time.sleep(2**attempt)
            print(f"verified {record['id']} ({path.stat().st_size / 1024**2:.1f} MiB)", flush=True)
        return {**record, "bytes": path.stat().st_size}

    with ThreadPoolExecutor(max_workers=3) as executor:
        records.extend(executor.map(download, remote))
    assets = {
        "ocr_det": "PP-OCRv6_det_small.onnx",
        "ocr_rec": "PP-OCRv6_rec_small.onnx",
        "ocr_dictionary": "ppocrv6_dict.txt",
        "orientation": "rapid_orientation.onnx",
        "seal_det": "pp-ocrv4_mobile_seal_det.onnx",
    }
    for identifier, filename in assets.items():
        source = package / "resources" / filename
        destination = target / filename
        if not destination.is_file() or sha256(source) != sha256(destination):
            shutil.copyfile(source, destination)
        records.append(
            {
                "id": identifier,
                "path": filename,
                "bytes": destination.stat().st_size,
                "sha256": sha256(destination),
                "source": f"rapid-doc==0.9.11/resources/{filename}",
                "license": "See THIRD_PARTY_NOTICES.md",
            }
        )
    for source in (package / "model" / "magika" / "models").rglob("*.onnx"):
        destination = target / "magika" / source.name
        destination.parent.mkdir(exist_ok=True)
        shutil.copyfile(source, destination)
        records.append(
            {
                "id": "file_classifier",
                "path": destination.relative_to(target).as_posix(),
                "bytes": destination.stat().st_size,
                "sha256": sha256(destination),
                "source": "rapid-doc==0.9.11/model/magika",
                "license": "Apache-2.0",
            }
        )
    manifest = {
        "schema_version": "1.0",
        "engine": "rapid-doc",
        "engine_version": "0.9.11",
        "profile": "cpu-ppdoclayoutv3-ppocrv6-formulanet-m-unet-slanet",
        "files": records,
        "total_bytes": sum(record["bytes"] for record in records),
    }
    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"Ready: {len(records)} assets, {manifest['total_bytes'] / 1024**2:.1f} MiB at {target}",
        flush=True,
    )


if __name__ == "__main__":
    main()
