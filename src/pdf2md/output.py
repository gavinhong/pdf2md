from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from .domain import safe_name


def table_markup(markdown: str, mode: str) -> str:
    if mode == "html":
        return markdown
    from bs4 import BeautifulSoup
    from markdownify import markdownify

    def convert(match: re.Match) -> str:
        soup = BeautifulSoup(match[0], "html.parser")
        table = soup.find("table")
        if table is None or table.find(["img", "math"]) or "$" in table.get_text():
            return match[0]
        if any(
            cell.get("rowspan", "1") != "1" or cell.get("colspan", "1") != "1"
            for cell in table.find_all(["td", "th"])
        ):
            return match[0]
        rows = table.find_all("tr")
        widths = {len(row.find_all(["th", "td"], recursive=False)) for row in rows}
        if len(widths) != 1 or not rows:
            return match[0]
        if not table.find("th"):
            head = soup.new_tag("tr")
            for index in range(next(iter(widths))):
                cell = soup.new_tag("th")
                cell.string = f"列 {index + 1}"
                head.append(cell)
            rows[0].insert_before(head)
        return "\n\n" + markdownify(str(table), table_infer_header=False).strip() + "\n\n"

    return re.sub(r"<table\b[\s\S]*?</table>", convert, markdown, flags=re.IGNORECASE)


def remap_resources(text: str, mapping: dict[str, str]) -> str:
    # Rewrite only Markdown image destinations and HTML src attributes.
    def markdown_image(match: re.Match) -> str:
        return match[1] + mapping.get(match[2], match[2]) + match[3]

    text = re.sub(r"(!\[[^\]]*\]\()([^\s)]+)(\))", markdown_image, text)
    return re.sub(r"""(\bsrc\s*=\s*["'])([^"']+)(["'])""", markdown_image, text)


def remap_object(value, mapping: dict[str, str]):
    if isinstance(value, dict):
        return {key: remap_object(item, mapping) for key, item in value.items()}
    if isinstance(value, list):
        return [remap_object(item, mapping) for item in value]
    if isinstance(value, str):
        return mapping.get(value, remap_resources(value, mapping))
    return value


def json_write(path: Path, payload) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )


def publish(staging: Path, output_root: Path, source_stem: str, cancelled=None) -> Path:
    from filelock import FileLock

    name = safe_name(source_stem)
    with FileLock(str(output_root / ".pdf2md-publish.lock"), timeout=20):
        if cancelled and cancelled():
            raise InterruptedError("任务已取消")
        candidate = output_root / name
        suffix = 2
        while candidate.exists():
            candidate = output_root / f"{name}_{suffix}"
            suffix += 1
        report_path = staging / "conversion-report.json"
        if report_path.is_file():
            report = json.loads(report_path.read_text("utf-8"))
            report["output"] = {
                "markdown": f"{candidate.name}.md",
                "images": "images/",
                "content_list": "content_list.json",
                "report": "conversion-report.json",
            }
            json_write(report_path, report)
        (staging / "document.md").rename(staging / f"{candidate.name}.md")
        staging.rename(candidate)
    return candidate


def clean_staging(staging: Path | None, output_root: Path) -> None:
    if staging is None:
        return
    resolved = staging.resolve()
    owned = (output_root / ".pdf2md-tmp").resolve()
    if resolved.is_relative_to(owned) and resolved != owned and resolved.is_dir():
        shutil.rmtree(resolved, ignore_errors=True)
