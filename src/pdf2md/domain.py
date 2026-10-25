from __future__ import annotations

import itertools
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PDFIUM_LOCK = threading.RLock()


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    output_dir: str = ""
    page_range: str = ""
    mode: Literal["auto", "ocr"] = "auto"
    extract_images: bool = True
    table_format: Literal["auto", "html"] = "auto"
    remove_headers: bool = True
    formula: bool = True


class Task(BaseModel):
    id: str
    source: str
    name: str
    size: int = 0
    pages: int = 0
    status: str = "draft"
    phase: str = "待开始"
    completed_pages: int = 0
    selected_pages: int = 0
    error: str = ""
    warnings: list[dict] = Field(default_factory=list)
    output_dir: str = ""
    markdown_file: str = ""
    elapsed: float = 0
    started_at: str = ""
    finished_at: str = ""
    source_sha256: str = ""
    reviewed_count: int = 0
    pending_count: int = 0


def timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_pages(expression: str, total: int) -> list[int]:
    if total < 1:
        raise ValueError("PDF 没有可转换的页面。")
    if not expression.strip():
        if total > 300:
            raise ValueError("一次最多转换 300 页，请填写较小的页码范围。")
        return list(range(1, total + 1))
    selected: set[int] = set()
    for token in expression.replace("，", ",").split(","):
        match = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", token)
        if not match:
            raise ValueError("页码格式有误，请使用 1-5,8,10-12。")
        start = int(match[1])
        end = int(match[2] or start)
        if start < 1 or end < start or end > total:
            raise ValueError(f"页码超出范围：本文件共 {total} 页，请填写 1–{total}。")
        if end - start + 1 > 300:
            raise ValueError("一次最多转换 300 页。")
        selected.update(range(start, end + 1))
        if len(selected) > 300:
            raise ValueError("一次最多转换 300 页。")
    return sorted(selected)


def page_runs(pages: list[int]) -> list[list[int]]:
    return [
        list(group)
        for _, group in itertools.groupby(pages, key=lambda n, c=itertools.count(): n - next(c))
    ]


def safe_name(name: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")[:90] or "document"
    if value.upper().split(".")[0] in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *[f"COM{i}" for i in range(1, 10)],
        *[f"LPT{i}" for i in range(1, 10)],
    }:
        value = "_" + value
    return value


def inspect_pdf(path: str) -> tuple[Path, int, int]:
    import pypdfium2 as pdfium

    source = Path(path).expanduser().resolve(strict=True)
    if not source.is_file() or source.suffix.lower() != ".pdf":
        raise ValueError("请选择 PDF 文件；当前版本不导入文件夹或其他格式。")
    size = source.stat().st_size
    if size > 100 * 1024 * 1024:
        raise ValueError("文件超过 100 MB，请使用较小的 PDF。")
    try:
        with PDFIUM_LOCK, pdfium.PdfDocument(source) as pdf:
            pages = len(pdf)
            if pdf.get_formtype() == 2:
                raise ValueError("暂不支持 XFA 表单 PDF，请先导出为普通 PDF。")
    except pdfium.PdfiumError as exc:
        if "password" in str(exc).lower():
            raise ValueError("PDF 需要密码；请先解密后再添加。") from exc
        raise ValueError("无法读取 PDF，请检查文件是否完整或重新导出。") from exc
    return source, size, pages
