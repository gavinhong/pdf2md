"""Source geometry and exact Markdown anchors without changing engine output.

All exported text offsets are UTF-16 code units in LF-normalized text. Geometry
uses the top-left origin of the PDF's displayed (including /Rotate) page.
"""

from __future__ import annotations

import functools
import hashlib
import importlib
import json
import math
import re
from collections import defaultdict
from collections.abc import Callable

IDENTITY = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def apply_affine(matrix: list[float], point: tuple | list) -> list[float]:
    a, b, c, d, e, f = matrix
    x, y = point
    return [a * x + b * y + c, d * x + e * y + f]


def inverse_affine(matrix: list[float]) -> list[float]:
    a, b, c, d, e, f = matrix
    determinant = a * e - b * d
    if abs(determinant) < 1e-12:
        raise ValueError("无法反向映射页面坐标。")
    return [e / determinant, -b / determinant, (b * f - e * c) / determinant,
            -d / determinant, a / determinant, (d * c - a * f) / determinant]


def rotation_mapping(size: tuple | list, angle: float, output_size=None) -> dict:
    """Match Pillow rotate(expand=True), including its pixel rounding/centering.

    The supplied sizes are the *actual* source/destination raster dimensions.
    For a pure PDF /Rotate correction normalized coordinates are independent of
    raster scale, so the displayed PDF dimensions can be used directly.
    """
    width, height = size
    angle %= 360.0
    radians = -math.radians(angle)
    a, b = round(math.cos(radians), 15), round(math.sin(radians), 15)
    d, e = -b, a
    c = width / 2 - a * width / 2 - b * height / 2
    f = height / 2 - d * width / 2 - e * height / 2
    if output_size is None:
        if angle in (90, 270):
            output_size = (height, width)
        elif angle in (0, 180):
            output_size = (width, height)
        else:
            corners = [apply_affine([a, b, c, d, e, f], point)
                       for point in ((0, 0), (width, 0), (width, height), (0, height))]
            output_size = (
                math.ceil(max(p[0] for p in corners)) - math.floor(min(p[0] for p in corners)),
                math.ceil(max(p[1] for p in corners)) - math.floor(min(p[1] for p in corners)),
            )
    out_width, out_height = output_size
    c, f = apply_affine([a, b, c, d, e, f],
                        (-(out_width - width) / 2, -(out_height - height) / 2))
    reverse = [a * out_width / width, b * out_height / width, c / width,
               d * out_width / height, e * out_height / height, f / height]
    return {"normalized_to_source": reverse, "source_to_normalized": inverse_affine(reverse)}


def source_region(page: int, bbox, corrections: dict) -> dict | None:
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None
    try:
        x0, y0, x1, y1 = [float(value) for value in bbox]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (x0, y0, x1, y1)) or x1 <= x0 or y1 <= y0:
        return None
    matrix = corrections.get(page, {}).get("normalized_to_source", IDENTITY)
    polygon = [apply_affine(matrix, point)
               for point in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
    # Clip the quadrilateral to the source page, rather than independently
    # clamping vertices (which would distort polygons crossing the page edge).
    for axis, boundary, keep_greater in ((0, 0, True), (0, 1, False),
                                         (1, 0, True), (1, 1, False)):
        clipped = []
        if not polygon:
            return None
        previous = polygon[-1]
        previous_inside = previous[axis] >= boundary if keep_greater else previous[axis] <= boundary
        for current in polygon:
            inside = current[axis] >= boundary if keep_greater else current[axis] <= boundary
            if inside != previous_inside:
                ratio = (boundary - previous[axis]) / (current[axis] - previous[axis])
                clipped.append([previous[i] + ratio * (current[i] - previous[i]) for i in (0, 1)])
            if inside:
                clipped.append(current)
            previous, previous_inside = current, inside
        polygon = clipped
    if len(polygon) < 3:
        return None
    return {"page": page, "polygon": [[round(v, 8) for v in point] for point in polygon],
            "bbox": [round(min(p[0] for p in polygon), 8), round(min(p[1] for p in polygon), 8),
                     round(max(p[0] for p in polygon), 8), round(max(p[1] for p in polygon), 8)]}


def mark_sources(pdf_info: list[dict]) -> None:
    """Called immediately before upstream para_split deep-copies source blocks."""
    for page in pdf_info:
        page_idx, size = page["page_idx"], page["page_size"]

        def visit(value, block_id):
            if isinstance(value, dict):
                value["_review_source"] = {"page_idx": page_idx, "size": list(size),
                                           "bbox": list(value.get("bbox", [])), "id": block_id}
                for key, item in list(value.items()):
                    if not key.startswith("_review_"):
                        visit(item, block_id)
            elif isinstance(value, list):
                for item in value:
                    visit(item, block_id)

        for index, block in enumerate(page.get("preproc_blocks", [])):
            visit(block, f"p{page_idx}-source-{index + 1}")


def collect_sources(block: dict) -> list[dict]:
    """Line/span provenance survives upstream cross-page paragraph merges."""
    found = []

    def visit(value):
        if isinstance(value, dict):
            source = value.get("_review_source")
            if source and len(source.get("bbox", [])) == 4:
                found.append(source)
            found.extend(value.get("_review_merged_sources", []))
            for key, item in value.items():
                if not key.startswith("_review_"):
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(block)
    # Prefer complete source-block regions, which remain useful for tables,
    # but retain line regions from other pages after text merging.
    unique = {}
    for source in found:
        key = (source["page_idx"], source["id"])
        existing = unique.get(key)
        if existing is None:
            unique[key] = dict(source, bbox=list(source["bbox"]))
        else:
            old, new = existing["bbox"], source["bbox"]
            existing["bbox"] = [min(old[0], new[0]), min(old[1], new[1]),
                                max(old[2], new[2]), max(old[3], new[3])]
    return list(unique.values())


def install_source_hooks() -> None:
    """Version-bound metadata wrappers; all parsing remains upstream's code."""
    module = importlib.import_module("rapid_doc.backend.pipeline.model_json_to_middle_json")
    if not getattr(module.para_split, "_pdf2md_review_hook", False):
        original = module.para_split

        @functools.wraps(original)
        def split(pdf_info):
            mark_sources(pdf_info)
            return original(pdf_info)

        split._pdf2md_review_hook = True
        module.para_split = split
    table_module = importlib.import_module("rapid_doc.utils.table_merge")
    if not getattr(table_module.perform_table_merge, "_pdf2md_review_hook", False):
        original_merge = table_module.perform_table_merge

        @functools.wraps(original_merge)
        def merge(previous_state, current_state, previous_table_block, wait_merge_table_footnotes):
            current_sources = collect_sources(current_state.owner_block)
            result = original_merge(previous_state, current_state, previous_table_block,
                                    wait_merge_table_footnotes)
            previous_table_block.setdefault("_review_merged_sources", []).extend(current_sources)
            return result

        merge._pdf2md_review_hook = True
        table_module.perform_table_merge = merge


class MappedMarkdown:
    """Track block ranges through the same exact replacements as the exporter."""

    def __init__(self, text=""):
        self.text = text
        self.anchors: dict[str, list[list[int]]] = defaultdict(list)

    def edit(self, start: int, end: int, replacement: str) -> None:
        delta = len(replacement) - (end - start)
        for ranges in self.anchors.values():
            for span in ranges:
                left, right = span
                if right <= start:
                    continue
                if left >= end:
                    span[:] = [left + delta, right + delta]
                else:
                    span[:] = [left if left < start else start,
                               right + delta if right > end else start + len(replacement)]
        self.text = self.text[:start] + replacement + self.text[end:]

    def sub(self, pattern: str, function: Callable, flags=0) -> None:
        edits = [(match.start(), match.end(), function(match))
                 for match in re.finditer(pattern, self.text, flags)]
        for start, end, replacement in reversed(edits):
            if replacement != self.text[start:end]:
                self.edit(start, end, replacement)

    def replace(self, old: str, new: str) -> None:
        self.sub(re.escape(old), lambda _match: new)

    def append(self, text: str, block_id="") -> None:
        start = len(self.text)
        self.text += text
        if block_id:
            self.anchors[block_id].append([start, len(self.text)])

    def extend(self, other: MappedMarkdown, separator="\n\n") -> None:
        if self.text:
            self.text += separator
        offset = len(self.text)
        self.text += other.text
        for key, spans in other.anchors.items():
            self.anchors[key].extend([[a + offset, b + offset] for a, b in spans])

    def remap_resources(self, mapping: dict[str, str]) -> None:
        def replace(match):
            return match[1] + mapping.get(match[2], match[2]) + match[3]

        self.sub(r"(!\[[^\]]*\]\()([^\s)]+)(\))", replace)
        self.sub(r"""(\bsrc\s*=\s*["'])([^"']+)(["'])""", replace)

    def finish(self, table_mode: str) -> None:
        from .output import table_markup

        stripped = self.text.rstrip()
        self.edit(len(stripped), len(self.text), "")
        start = len(self.text) - len(self.text.lstrip())
        self.edit(0, start, "")
        self.text += "\n"
        self.sub(r"<table\b[\s\S]*?</table>",
                 lambda match: table_markup(match[0], table_mode), re.IGNORECASE)

    def utf16_ranges(self) -> dict[str, list[list[int]]]:
        points = sorted({point for spans in self.anchors.values() for span in spans for point in span})
        offsets, previous, total = {}, 0, 0
        for point in points:
            total += utf16_length(self.text[previous:point])
            offsets[point], previous = total, point
        return {key: [[offsets[a], offsets[b]] for a, b in spans if b > a]
                for key, spans in self.anchors.items()}


def attach_engine_mapping(raw_pages: list[dict], content: list[dict], markdown: str,
                          page_map: list[int], corrections: dict, mode: str) -> MappedMarkdown:
    """Index exactly the blocks used by RapidDoc's own union_make exporter."""
    from rapid_doc.backend.pipeline.pipeline_middle_json_mkcontent import (
        make_blocks_to_content_list,
        make_blocks_to_markdown,
    )

    mapped = MappedMarkdown(markdown)
    emitted, offset, record_index = [], 0, 0
    associations = []
    for page in raw_pages:
        for raw in list(page.get("para_blocks") or []) + list(page.get("discarded_blocks") or []):
            exported = make_blocks_to_content_list(raw, "images", page["page_idx"], page["page_size"])
            if not exported:
                continue
            if record_index >= len(content):
                return MappedMarkdown(markdown)
            record = content[record_index]
            record_index += 1
            # Validate the exact content-list position, including discarded blocks.
            if any(exported.get(key) != record.get(key) for key in ("type", "text", "table_body")):
                return MappedMarkdown(markdown)
            sources = collect_sources(raw)
            if not sources and raw.get("bbox"):
                sources = [{"page_idx": page["page_idx"], "size": page["page_size"],
                            "bbox": raw["bbox"], "id": ""}]
            regions, source_ids = [], []
            for source in sources:
                local_page = source["page_idx"]
                if not 0 <= local_page < len(page_map):
                    continue
                size = source["size"]
                bbox = [v / size[i % 2] for i, v in enumerate(source["bbox"])]
                region = source_region(page_map[local_page], bbox, corrections)
                if region and region not in regions:
                    regions.append(region)
                if source["id"]:
                    source_ids.append(source["id"])
            record["source_regions"] = regions
            record["_source_ids"] = source_ids
            raw["_review_block_id"] = record["id"]
            # Discarded blocks are intentionally absent from upstream Markdown.
            if raw in (page.get("discarded_blocks") or []):
                continue
            pieces = make_blocks_to_markdown([raw], mode, "images")
            for piece in pieces:
                if emitted:
                    offset += 2
                associations.append((record["id"], offset, offset + len(piece)))
                emitted.append(piece)
                offset += len(piece)
    if record_index != len(content) or "\n\n".join(emitted) != markdown:
        return MappedMarkdown(markdown)
    for block_id, start, end in associations:
        mapped.anchors[block_id].append([start, end])
    return mapped


def finalize_mapping(mapped: MappedMarkdown, blocks: list[dict], warnings: list[dict],
                     pages: list[dict], corrections: dict) -> None:
    ranges = mapped.utf16_ranges()
    by_id = {block["id"]: block for block in blocks}
    by_source = defaultdict(list)
    for block in blocks:
        block["markdown_ranges"] = ranges.get(block["id"], [])
        for source_id in block.pop("_source_ids", []):
            by_source[(block.get("_run_start", 0), source_id)].append(block)
    duplicates = defaultdict(int)
    for warning in warnings:
        linked = [by_id[item] for item in warning.get("block_ids", []) if item in by_id]
        if not linked and warning.get("_source_id"):
            linked = by_source.get((warning.get("_run_start", 0), warning["_source_id"]), [])
            linked = [block for block in linked if block.get("markdown_ranges")]
        warning["block_ids"] = list(dict.fromkeys(block["id"] for block in linked))
        warning["markdown_ranges"] = [span for block in linked for span in block["markdown_ranges"]]
        if warning.get("_anchor_id"):
            warning["markdown_ranges"].extend(ranges.get(warning["_anchor_id"], []))
        if not warning.get("source_regions"):
            region = source_region(warning["page"], warning.get("bbox"), corrections)
            warning["source_regions"] = [region] if region else []
        normalized_bbox = warning.get("bbox")
        if normalized_bbox:
            warning["normalized_bbox"] = normalized_bbox
        if warning["source_regions"]:
            region = warning["source_regions"][0]
            warning["page"], warning["bbox"] = region["page"], region["bbox"]
        identity = {key: warning.get(key) for key in ("code", "page", "bbox", "block_ids", "image")}
        key = json.dumps(identity, sort_keys=True, ensure_ascii=False)
        duplicates[key] += 1
        warning["id"] = "w-" + hashlib.sha256(f"{key}:{duplicates[key]}".encode()).hexdigest()[:20]
        for key in list(warning):
            if key.startswith("_"):
                warning.pop(key)
    for page in pages:
        page["markdown_ranges"] = [span for block in blocks
                                   if any(region["page"] == page["page"]
                                          for region in block.get("source_regions", []))
                                   for span in block["markdown_ranges"]]
    for block in blocks:
        block.pop("_run_start", None)
