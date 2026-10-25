"""Local review records, validated text changes, and crash-safe file replacement."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import tempfile
import uuid
from pathlib import Path

from .domain import timestamp

MAX_DOCUMENT_BYTES = 32 * 1024 * 1024


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".pdf2md-", delete=False) as f:
            temporary = Path(f.name)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


def read_json(path: Path) -> dict:
    try:
        result = json.loads(path.read_text("utf-8"))
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return {}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def ranges(value: dict) -> list[list[int]]:
    result = value.get("markdown_ranges")
    if result is None:
        single = value.get("markdown_range")
        if isinstance(single, dict):
            result = [[single.get("start"), single.get("end")]]
        elif isinstance(single, list):
            result = [single]
    if not isinstance(result, list):
        return []
    return [
        [v[0], v[1]] for v in (result or [])
        if isinstance(v, list) and len(v) == 2
        and all(isinstance(p, int) and not isinstance(p, bool) for p in v)
        and 0 <= v[0] <= v[1]
    ]


def polygon_from_bbox(bbox) -> list[list[float]]:
    if not isinstance(bbox, list) or len(bbox) != 4:
        return []
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in bbox):
        return []
    x0, y0, x1, y1 = [max(0, min(1, float(v))) for v in bbox]
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def normalize_document(task, markdown: str, content: dict, report: dict) -> dict:
    """Normalize additive 1.1 fields and conservatively read older exports."""
    pages = content.get("pages") or []
    pages = [p for p in pages if isinstance(p, dict)] if isinstance(pages, list) else []
    page_map = {p.get("page"): p for p in pages if isinstance(p.get("page"), int)}
    raw_blocks = content.get("blocks") or []
    blocks = copy.deepcopy([b for b in raw_blocks if isinstance(b, dict)]
                           if isinstance(raw_blocks, list) else [])
    by_id = {}
    for index, block in enumerate(blocks):
        if not isinstance(block.get("id"), str):
            block["id"] = f"legacy-block-{index}"
        block["markdown_ranges"] = ranges(block)
        if not block["markdown_ranges"]:
            text = block.get("table_body") or block.get("text") or ""
            if isinstance(text, str) and text and markdown.count(text) == 1:
                start = markdown.index(text)
                block["markdown_ranges"] = [[
                    utf16_length(markdown[:start]), utf16_length(markdown[:start + len(text)])
                ]]
        by_id[block["id"]] = block
    raw_warnings = content.get("warnings", report.get("warnings", task.warnings)) or []
    warnings = copy.deepcopy([w for w in raw_warnings if isinstance(w, dict)]
                             if isinstance(raw_warnings, list) else [])
    for index, warning in enumerate(warnings):
        if not isinstance(warning.get("id"), str):
            warning["id"] = "w-" + digest(json_bytes(warning))[:16] + f"-{index}"
        warning["markdown_ranges"] = ranges(warning)
        warning["block_ids"] = warning.get("block_ids") or (
            [warning["block_id"]] if warning.get("block_id") else []
        )
        if not isinstance(warning["block_ids"], list):
            warning["block_ids"] = []
        warning["block_ids"] = [v for v in warning["block_ids"] if isinstance(v, str)]
        if not warning["block_ids"]:
            candidates = [b for b in blocks if b.get("page") == warning.get("page")]
            image = warning.get("image")
            if image:
                matched = [b for b in candidates if image in json.dumps(b, ensure_ascii=False)]
                if len(matched) == 1:
                    warning["block_ids"] = [matched[0]["id"]]
        if not warning["markdown_ranges"]:
            warning["markdown_ranges"] = [
                r for key in warning["block_ids"] for r in by_id.get(key, {}).get("markdown_ranges", [])
            ]
        if not isinstance(warning.get("source_regions"), list):
            page = page_map.get(warning.get("page"), {})
            corrected = page.get("rotation_correction") or page.get("deskew_correction")
            poly = polygon_from_bbox(warning.get("bbox")) if not corrected else []
            warning["source_regions"] = (
                [{"page": warning["page"], "polygon": poly}] if poly else []
            )
        warning["reviewed"] = False
    return {"blocks": blocks, "warnings": warnings, "pages": pages}


def load_document(task) -> dict:
    path = Path(task.markdown_file)
    if not path.is_file():
        raise ValueError("结果文件不存在，可能已移动或删除。请打开已有转换结果。")
    if path.stat().st_size > MAX_DOCUMENT_BYTES:
        raise ValueError("Markdown 超过 32 MiB，无法在界面完整编辑；原文件保持不变。")
    data = path.read_bytes()
    markdown = data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    sha = digest(data)
    content = read_json(path.parent / "content_list.json")
    report = read_json(path.parent / "conversion-report.json")
    result = normalize_document(task, markdown, content, report)
    state = read_json(path.parent / "review-state.json")
    notice = ""
    malformed = bool(state) and (
        not isinstance(state.get("warnings"), list)
        or not isinstance(state.get("blocks"), list)
        or not isinstance(state.get("revision"), str)
        or any(not isinstance(w, dict) or not isinstance(w.get("id"), str)
               or not isinstance(w.get("markdown_ranges"), list)
               or not isinstance(w.get("reviewed"), bool) for w in state.get("warnings") or [])
        or any(not isinstance(b, dict) or not isinstance(b.get("id"), str)
               or not isinstance(b.get("markdown_ranges"), list)
               for b in state.get("blocks") or [])
    )
    if malformed:
        state = {}
        notice = "核对记录损坏，已保留正文并恢复待核对状态。"
    if state and state.get("sha256") == sha:
        saved = {w["id"]: w for w in state.get("warnings", [])}
        for warning in result["warnings"]:
            if warning["id"] in saved:
                warning.update({k: v for k, v in saved[warning["id"]].items()
                                if k in ("reviewed", "reviewed_at", "markdown_ranges")})
                warning["markdown_ranges"] = ranges(warning)
        previous_blocks = {b["id"]: b for b in state.get("blocks", [])}
        for block in result["blocks"]:
            if block["id"] in previous_blocks:
                block["markdown_ranges"] = ranges(previous_blocks[block["id"]])
        revision = state.get("revision", sha)
    else:
        baseline = state.get("sha256") or content.get("markdown_sha256")
        if baseline and baseline != sha:
            notice = "正文已被外部修改，原有文本定位及核对标记已失效，请重新核对。"
            for item in result["blocks"] + result["warnings"]:
                item["markdown_ranges"] = []
        revision = digest((sha + str(state.get("revision", "")) + json.dumps(
            [w["id"] for w in result["warnings"]]
        )).encode())
    if not notice and any(not w["markdown_ranges"] or not w["source_regions"]
                          for w in result["warnings"]):
        notice = "部分提示缺少精确定位信息，已保留页级定位；重新转换可获得完整定位信息。"
    result.update(ok=True, markdown=markdown, sha256=sha, review_revision=revision,
                  notice=notice, source_metadata=(content.get("source")
                    if isinstance(content.get("source"), dict) else {}))
    result["reviewed_count"] = sum(bool(w.get("reviewed")) for w in result["warnings"])
    result["pending_count"] = len(result["warnings"]) - result["reviewed_count"]
    return result


def apply_changes(old: str, changes: list, expected: str) -> list[dict]:
    if not isinstance(changes, list) or len(changes) > 100_000:
        raise ValueError("编辑变更记录无效，请保留草稿并重新载入。")
    raw = old.encode("utf-16-le")
    limit = len(raw) // 2
    end = 0
    chunks, valid = [], []
    for change in changes:
        if not isinstance(change, dict):
            raise ValueError("编辑变更记录格式无效。")
        start, stop, insert = change.get("from"), change.get("to"), change.get("insert")
        if (not isinstance(start, int) or isinstance(start, bool)
                or not isinstance(stop, int) or isinstance(stop, bool)
                or not end <= start <= stop <= limit or not isinstance(insert, str)):
            raise ValueError("编辑变更位置无效，请保留草稿并重新载入。")
        # Decoding each unchanged segment rejects a boundary inside a surrogate pair.
        try:
            chunks.append(raw[end * 2:start * 2].decode("utf-16-le"))
            raw[start * 2:stop * 2].decode("utf-16-le")
        except UnicodeError as exc:
            raise ValueError("编辑变更切断了完整字符，请重新载入。") from exc
        chunks.append(insert)
        valid.append({"from": start, "to": stop, "insert": insert,
                      "length": utf16_length(insert)})
        end = stop
    chunks.append(raw[end * 2:].decode("utf-16-le"))
    if "".join(chunks) != expected:
        raise ValueError("编辑内容与变更记录不一致，未保存，请保留草稿并重新载入。")
    return valid


def map_position(position: int, changes: list[dict], end: bool = False) -> int:
    shift = 0
    for c in changes:
        if position < c["from"]:
            break
        if position == c["to"] and c["to"] > c["from"]:
            return c["from"] + shift + c["length"]
        if position <= c["to"]:
            return c["from"] + shift + (c["length"] if end else 0)
        shift += c["length"] - (c["to"] - c["from"])
    return position + shift


def updated_ranges(values: list[list[int]], changes: list[dict]) -> list[list[int]]:
    return [[map_position(a, changes), map_position(b, changes, True)] for a, b in values]


def intersects(values: list[list[int]], changes: list[dict]) -> bool:
    return any(c["from"] <= b and c["to"] >= a for a, b in values for c in changes)


def save_document(task, payload: dict) -> dict:
    from filelock import FileLock

    if not isinstance(payload, dict) or not isinstance(payload.get("markdown"), str):
        return {"ok": False, "error": "保存内容格式无效。"}
    text = payload["markdown"].replace("\r\n", "\n").replace("\r", "\n")
    try:
        data = text.encode("utf-8")
    except UnicodeError:
        return {"ok": False, "error": "正文包含不完整字符，未保存。"}
    if len(data) > MAX_DOCUMENT_BYTES:
        return {"ok": False, "error": "正文超过 32 MiB，未保存；请保留草稿。"}
    path = Path(task.markdown_file)
    markdown_saved = False
    sha = ""
    try:
        with FileLock(str(path.parent / ".pdf2md-review.lock"), timeout=10):
            current = load_document(task)
            if (payload.get("expected_sha256") != current["sha256"]
                    or payload.get("expected_revision") != current["review_revision"]):
                return {"ok": False, "conflict": True,
                        "error": "文件或核对进度已被修改，请保留草稿并重新载入。"}
            changes = apply_changes(current["markdown"], payload.get("changes", []), text)
            updates = payload.get("review_updates", {})
            known = {w["id"] for w in current["warnings"]}
            if not isinstance(updates, dict) or any(
                k not in known or not isinstance(v, bool) for k, v in updates.items()
            ):
                raise ValueError("核对提示不存在或核对状态无效。")
            touched = set()
            for block in current["blocks"]:
                if intersects(block["markdown_ranges"], changes):
                    touched.add(block["id"])
                block["markdown_ranges"] = updated_ranges(block["markdown_ranges"], changes)
            for warning in current["warnings"]:
                affected = bool(set(warning["block_ids"]) & touched)
                affected |= intersects(warning["markdown_ranges"], changes)
                # Older results without text ranges cannot distinguish affected regions.
                affected |= bool(changes and not warning["markdown_ranges"])
                if affected:
                    warning["reviewed"] = False
                    warning.pop("reviewed_at", None)
                warning["markdown_ranges"] = updated_ranges(warning["markdown_ranges"], changes)
                if warning["id"] in updates:
                    warning["reviewed"] = updates[warning["id"]]
                    warning["reviewed_at"] = timestamp() if warning["reviewed"] else ""
            sha = digest(data)
            # The original conversion remains recoverable after the first manual edit.
            backup = path.with_name(path.stem + ".original.md")
            original_bytes = path.read_bytes()
            if digest(original_bytes) != current["sha256"]:
                return {"ok": False, "conflict": True,
                        "error": "保存期间文件被外部修改，未覆盖；请保留草稿并重新载入。"}
            if data != original_bytes:
                if not backup.exists():
                    atomic_write(backup, original_bytes)
                if file_digest(path) != current["sha256"]:
                    return {"ok": False, "conflict": True,
                            "error": "保存期间文件被外部修改，未覆盖；请保留草稿并重新载入。"}
                atomic_write(path, data)
            markdown_saved = True
            state = {"schema_version": "1.0", "sha256": sha, "revision": uuid.uuid4().hex,
                     "saved_at": timestamp(), "blocks": [
                         {"id": b["id"], "markdown_ranges": b["markdown_ranges"]}
                         for b in current["blocks"]
                     ], "warnings": [{k: w.get(k) for k in
                                      ("id", "reviewed", "reviewed_at", "markdown_ranges")}
                                     for w in current["warnings"]]}
            atomic_write(path.parent / "review-state.json", json_bytes(state))
        return load_document(task)
    except (OSError, ValueError, UnicodeError, TimeoutError) as exc:
        return {"ok": False, "markdown_saved": markdown_saved, "sha256": sha,
                "error": ("正文已保存，但核对进度保存失败。请重新载入后核对。" if markdown_saved
                          else "保存失败，编辑内容已保留：") + str(exc)[:400]}
