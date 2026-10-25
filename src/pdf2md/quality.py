"""Inspect existing engine evidence without inventing confidence or rewriting content."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup
from pylatexenc.latexwalker import LatexWalker, LatexWalkerParseError

MATH = re.compile(r"\$\$([\s\S]*?)\$\$|(?<!\\)\$(?!\$)((?:\\.|[^$\n])+?)\$")


def invalid_formulas(text: str) -> list[str]:
    invalid = []
    for match in MATH.finditer(text):
        latex = match[1] if match[1] is not None else match[2]
        try:
            LatexWalker(latex, tolerant_parsing=False).get_latex_nodes(pos=0)
        except (LatexWalkerParseError, ValueError):
            invalid.append(match[0])
    return invalid


def ocr_evidence(page: dict, number: int, source_blocks: bool = False) -> tuple[list[dict], list[float]]:
    warnings, scores = [], []
    width, height = page.get("page_size", [1, 1])

    def visit(value):
        if isinstance(value, dict):
            score = value.get("score")
            if (
                value.get("type") == "text"
                and "content" in value
                and isinstance(score, (int, float))
            ):
                scores.append(float(score))
                if score < 0.80:
                    bbox = value.get("bbox", [])
                    normalized = [
                        round(v / (width if i % 2 == 0 else height), 4) for i, v in enumerate(bbox)
                    ]
                    warning = {
                        "code": "low_ocr_confidence",
                        "page": number,
                        "bbox": normalized,
                        "confidence": round(float(score), 3),
                        "message": "该区域文字识别置信度偏低，请对照原文核对。",
                    }
                    source = value.get("_review_source", {})
                    if source.get("id"):
                        warning["_source_id"] = source["id"]
                    warnings.append(warning)
            for key, item in value.items():
                if key not in ("discarded_blocks",) and not key.startswith("_review_"):
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    # Pre-merge blocks retain the actual physical page and source spans. The
    # final para_blocks may contain spans moved from the following page.
    key = "preproc_blocks" if source_blocks and "preproc_blocks" in page else "para_blocks"
    visit(page.get(key, []))
    return warnings, scores


def continuation_warnings(blocks: list[dict]) -> list[dict]:
    warnings = []
    tables = [block for block in blocks if block.get("type") == "table" and block.get("table_body")]
    for previous, current in zip(tables, tables[1:]):
        if current["page"] != previous["page"] + 1:
            continue
        if previous.get("bbox", [0, 0, 0, 0])[3] < 0.65 or current.get("bbox", [0, 1])[1] > 0.35:
            continue

        def columns(block):
            first = BeautifulSoup(block["table_body"], "html.parser").find("tr")
            return (
                sum(int(cell.get("colspan", 1)) for cell in first.find_all(["td", "th"]))
                if first
                else 0
            )

        if columns(previous) == columns(current) and columns(current):
            current.setdefault("warnings", []).append("possible_table_continuation")
            warnings.append(
                {
                    "code": "possible_table_continuation",
                    "page": current["page"],
                    "bbox": current.get("bbox"),
                    "block_ids": [current["id"]] if current.get("id") else [],
                    "source_regions": current.get("source_regions", []),
                    "previous_page": previous["page"],
                    "message": "可能为上一页续表，已保留表格分段，请核对表头与衔接。",
                }
            )
    return warnings
