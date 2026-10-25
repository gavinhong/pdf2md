"""Refresh release documents only after functional ZIP verification has passed.

This does not build or run the application. It checks the final ZIP's entire
non-document payload against the independently extracted, tested application
before atomically replacing the archive and checksum. No directory is removed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = (
    "README.md",
    "REQUIREMENTS.md",
    "TECHNICAL_DESIGN.md",
    "docs/verification.md",
    "docs/startup.md",
)
MANIFEST = "package-manifest.json"
ALLOWED_DIFFERENCES = frozenset((*DOCUMENTS, MANIFEST))


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".pdf2md-docs-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.is_file():
            temporary.unlink()


def json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


def files_under(base: Path) -> dict[str, Path]:
    base = base.resolve(strict=True)
    result = {}
    for path in base.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Package contains a symbolic link: {path}")
        if not path.is_file():
            continue
        if not path.resolve().is_relative_to(base):
            raise ValueError(f"Package contains an external link: {path}")
        result[path.relative_to(base).as_posix()] = path
    return result


def compare_payload(current: dict[str, Path], tested: dict[str, Path]) -> dict[str, str]:
    current_payload = set(current) - ALLOWED_DIFFERENCES
    tested_payload = set(tested) - ALLOWED_DIFFERENCES
    if current_payload != tested_payload:
        raise ValueError(
            "Non-document file set differs from the tested extraction: "
            f"added={sorted(current_payload - tested_payload)}, "
            f"missing={sorted(tested_payload - current_payload)}"
        )
    expected = {}
    for name in sorted(tested_payload):
        tested_path, current_path = tested[name], current[name]
        if current_path.stat().st_size != tested_path.stat().st_size:
            raise ValueError(f"Non-document file size differs: {name}")
        expected[name] = digest(tested_path)
        if digest(current_path) != expected[name]:
            raise ValueError(f"Non-document file SHA-256 differs: {name}")
    if not expected:
        raise ValueError("The tested package has no non-document payload.")
    return expected


def verify_new_archive(
    path: Path, files: dict[str, Path], expected: dict[str, str], documents: dict[str, bytes]
) -> None:
    """Read every member to EOF, validating ZIP CRC as well as payload SHA-256."""
    with zipfile.ZipFile(path) as archive:
        seen = set()
        for entry in archive.infolist():
            member = PurePosixPath(entry.filename)
            if (
                entry.is_dir()
                or member.is_absolute()
                or ".." in member.parts
                or "\\" in entry.filename
                or len(member.parts) < 2
                or member.parts[0] != "PDF2MD"
            ):
                raise ValueError(f"Unexpected archive member: {entry.filename}")
            name = PurePosixPath(*member.parts[1:]).as_posix()
            if name in seen or name not in files:
                raise ValueError(f"Duplicate or unexpected archive member: {name}")
            seen.add(name)
            if entry.file_size != files[name].stat().st_size:
                raise ValueError(f"Archive member size changed: {name}")
            with archive.open(entry) as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if name in expected:
                if actual != expected[name]:
                    raise ValueError(f"Archive payload differs from the tested extraction: {name}")
            elif name in documents:
                if actual != hashlib.sha256(documents[name]).hexdigest():
                    raise ValueError(f"Refreshed document changed during packaging: {name}")
            elif name == MANIFEST:
                if actual != digest(files[name]):
                    raise ValueError("Package manifest changed during packaging.")
            else:
                raise ValueError(f"A new non-document file appeared during packaging: {name}")
        if seen != set(files):
            raise ValueError(f"Archive is incomplete: {sorted(set(files) - seen)}")


def refresh(version: str) -> dict:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Version must have the form major.minor.patch.")
    target = ROOT / "dist/PDF2MD"
    tested = ROOT / f"output/package-check-{version}/PDF2MD"
    archive = ROOT / f"dist/PDF2MD-win32-AMD64-{version}.zip"
    checksum_path = archive.with_suffix(".zip.sha256")
    staged_archive = archive.with_suffix(".zip.new")
    verification = ROOT / f"output/qa/package-check-{version}.json"
    record = json.loads(verification.read_text("utf-8"))
    if not isinstance(record, dict) or not record.get("ok") or record.get("archive") != archive.name:
        raise ValueError("The requested archive does not have a successful functional verification record.")
    if staged_archive.exists():
        raise FileExistsError(f"A staged archive already exists; inspect it before retrying: {staged_archive}")
    original_sha = digest(archive)
    if original_sha != record.get("sha256"):
        raise ValueError("The current archive differs from its functional verification record.")
    if checksum_path.read_text("utf-8").split()[0] != original_sha:
        raise ValueError("The current archive checksum sidecar does not match.")

    current_files, tested_files = files_under(target), files_under(tested)
    expected_payload = compare_payload(current_files, tested_files)
    manifest = json.loads((target / MANIFEST).read_text("utf-8"))
    if not isinstance(manifest, dict) or not manifest.get("platform") or not manifest.get("architecture"):
        raise ValueError("The existing package manifest has no platform/architecture metadata.")
    documents = {name: (ROOT / name).read_bytes() for name in DOCUMENTS}
    for name, data in documents.items():
        if name not in current_files:
            raise ValueError(f"The built application lacks a required document: {name}")
        atomic_write(target / name, data)

    files = files_under(target)
    manifest_records = [
        {"path": name, "bytes": path.stat().st_size}
        for name, path in sorted(files.items()) if name != MANIFEST
    ]
    manifest.update(total_bytes=sum(item["bytes"] for item in manifest_records), files=manifest_records)
    atomic_write(target / MANIFEST, json_bytes(manifest))
    files = files_under(target)

    with zipfile.ZipFile(staged_archive, "x", zipfile.ZIP_DEFLATED, compresslevel=6) as output:
        for name, path in sorted(files.items()):
            output.write(path, f"PDF2MD/{name}")
    verify_new_archive(staged_archive, files, expected_payload, documents)
    final_sha, final_bytes = digest(staged_archive), staged_archive.stat().st_size
    # Only individually atomic file replacements are possible for this pair.
    # Both files are replaced only after the complete candidate has verified.
    os.replace(staged_archive, archive)
    atomic_write(checksum_path, f"{final_sha}  {archive.name}\n".encode("utf-8"))
    record.update(
        functional_test_archive_sha256=record.get("functional_test_archive_sha256", original_sha),
        archive_bytes=final_bytes,
        sha256=final_sha,
        final_crc_verified=True,
        unchanged_payload_files=len(expected_payload),
        docs_refreshed=True,
        refreshed_documents=list(DOCUMENTS),
        docs_refreshed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    atomic_write(verification, json_bytes(record))
    atomic_write(ROOT / "dist/verification-results.json", json_bytes(record))
    return {
        "archive": archive.name,
        "archive_bytes": final_bytes,
        "sha256": final_sha,
        "functional_test_archive_sha256": record["functional_test_archive_sha256"],
        "unchanged_payload_files": len(expected_payload),
        "docs_refreshed": True,
        "final_crc_verified": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="0.2.1")
    args = parser.parse_args()
    print(json.dumps(refresh(args.version), ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
