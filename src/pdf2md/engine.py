"""Thin, version-bound integration with RapidDoc's existing parsing pipeline."""

from __future__ import annotations

import functools
import hashlib
import importlib
import os
import platform
import re
import socket
import time
from pathlib import Path

from . import __version__
from .domain import Settings, page_runs, timestamp
from .models import check_models
from .output import json_write, remap_object
from .quality import continuation_warnings, invalid_formulas, ocr_evidence
from .review_mapping import (
    IDENTITY,
    MappedMarkdown,
    attach_engine_mapping,
    finalize_mapping,
    install_source_hooks,
    rotation_mapping,
)


def restrict_network() -> None:
    """Inference is local; prevent implicit model downloads in this worker only."""
    original = socket.socket.connect

    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            host = str(address[0])
            if host not in ("127.0.0.1", "::1", "localhost"):
                raise OSError("离线转换不允许联网；请检查本地模型是否齐全。")
        return original(sock, address)

    socket.socket.connect = connect


class RapidDocAdapter:
    def __init__(self, root: Path, emit):
        self.info = check_models(root, full=True)
        self.emit = emit
        self.engines = {}
        self.window_done = 0
        self.total = 0
        self._hooked = False
        self._orientation = None
        self._warmed_profiles: set[tuple[bool, bool]] = set()
        os.environ["MINERU_DEVICE_MODE"] = "cpu"
        os.environ["RAPID_MODELS_DIR"] = str(root)
        os.environ["MINERU_MIN_BATCH_INFERENCE_SIZE"] = "4"
        os.environ["USE_DOC_ORIENTATION_CLASSIFY"] = "false"
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TOKENIZERS_PARALLELISM"] = "false"

    def phase(self, label: str) -> None:
        self.emit({"type": "phase", "phase": label})

    def warmup(self, settings: Settings) -> dict:
        from .warming import prepare_pipeline, warmup_profile

        profile = warmup_profile(settings)
        key = (profile["remove_headers"], profile["formula"])
        if key not in self._warmed_profiles:
            engine = self.configure(settings)
            orientation = prepare_pipeline(engine, settings, self.phase)
            # The table pipeline and our page-normalization path share the
            # same already-loaded orientation engine after desktop warmup.
            if self._orientation is None:
                self._orientation = orientation
            self._warmed_profiles.add(key)
        return profile

    def _get_orientation(self):
        if self._orientation is None:
            from rapid_doc.model.orientation.rapid_orientation import RapidOrientation

            self._orientation = RapidOrientation(model_path=self.info["paths"]["orientation"])
        return self._orientation

    def configure(self, settings: Settings):
        from rapid_doc import RapidDoc
        from rapid_doc.model.formula.rapid_formula_self import EngineType as FormulaEngine
        from rapid_doc.model.formula.rapid_formula_self import ModelType as FormulaType
        from rapid_doc.model.table.rapid_table_self import EngineType as TableEngine
        from rapid_doc.model.table.rapid_table_self import ModelType as TableType

        paths = self.info["paths"]
        key = (settings.remove_headers, settings.formula)
        if key in self.engines:
            return self.engines[key]
        self.phase("加载本地识别模型")
        threads = max(1, min(4, (os.cpu_count() or 4) // 2))
        cpu = {
            "use_cuda": False,
            "use_dml": False,
            "intra_op_num_threads": threads,
            "inter_op_num_threads": 1,
            "onnx_ops": False,
        }
        ignore = (
            ["number", "header", "header_image", "footer", "footer_image"]
            if settings.remove_headers
            else []
        )
        engine = RapidDoc(
            layout_config={
                "model_type": "pp_doclayoutv3",
                "model_dir_or_path": paths["layout"],
                "engine_cfg": cpu,
                "markdown_ignore_labels": ignore,
            },
            ocr_config={
                "Det.model_path": paths["ocr_det"],
                "Rec.model_path": paths["ocr_rec"],
                "Rec.rec_keys_path": paths["ocr_dictionary"],
                "onnx_ops": False,
                "seal_enable": False,
                "EngineConfig.onnxruntime.intra_op_num_threads": threads,
                "EngineConfig.onnxruntime.inter_op_num_threads": 1,
            },
            formula_config={
                "model_type": FormulaType.PP_FORMULANET_PLUS_M,
                "engine_type": FormulaEngine.ONNXRUNTIME,
                "model_dir_or_path": paths["formula"],
                "engine_cfg": cpu,
            },
            table_config={
                "model_type": TableType.UNET_SLANET_PLUS,
                "engine_type": TableEngine.ONNXRUNTIME,
                "engine_cfg": cpu,
                "cls.model_dir_or_path": {
                    "paddle_cls": paths["table_paddle_cls"],
                    "q_cls": paths["table_q_cls"],
                },
                "unet.model_dir_or_path": paths["table_unet"],
                "slanet_plus.model_dir_or_path": paths["table_slanet"],
                "extract_original_image": True,
            },
            image_config={"extract_original_image": True},
            formula_enable=settings.formula,
            table_enable=True,
            pdf_pages_batch=4,
        )
        self._install_progress_hooks()
        self.engines[key] = engine
        return engine

    def _install_progress_hooks(self) -> None:
        if self._hooked:
            return
        from rapid_doc.backend.pipeline.batch_analyze import BatchAnalyze

        install_source_hooks()
        phases = {
            "_run_layout_detection": "分析版面与阅读顺序",
            "_run_formula_recognition": "识别数学公式",
            "_run_traditional_ocr": "识别正文文字",
            "_run_table_recognition": "识别表格结构",
        }
        for name, label in phases.items():
            original = getattr(BatchAnalyze, name)

            def wrap(function, phase):
                @functools.wraps(function)
                def observed(*args, **kwargs):
                    self.phase(phase)
                    return function(*args, **kwargs)

                return observed

            setattr(BatchAnalyze, name, wrap(original, label))
        module = importlib.import_module("rapid_doc.main")
        original = module.pipeline_result_to_middle_json

        @functools.wraps(original)
        def completed(*args, **kwargs):
            result = original(*args, **kwargs)
            for page in result.get("pdf_info", []):
                page["_reading_method"] = (
                    "ocr" if (args[5] if len(args) > 5 else kwargs.get("ocr_enable")) else "native"
                )
            self.window_done += len(result.get("pdf_info", []))
            self.emit(
                {
                    "type": "progress",
                    "completed_pages": min(self.window_done, self.total),
                    "selected_pages": self.total,
                }
            )
            return result

        module.pipeline_result_to_middle_json = completed
        self._hooked = True

    def normalize_orientation(
        self, source: Path, pages: list[int], staging: Path
    ) -> tuple[Path, dict]:
        import numpy as np
        import pypdfium2 as pdfium
        from jdeskew.estimator import get_angle
        from PIL import Image

        self.phase("检查页面方向与倾斜")
        orientation = self._get_orientation()
        corrections = {}
        corrected = None
        with pdfium.PdfDocument(source) as document:
            try:
                for number in pages:
                    page = document[number - 1]
                    source_size = list(page.get_size())
                    corrections[number] = {
                        "rotation": 0, "deskew": 0, "source_size": source_size,
                        "normalized_size": source_size, "source_to_normalized": list(IDENTITY),
                        "normalized_to_source": list(IDENTITY),
                    }
                    text_page = page.get_textpage()
                    has_text = text_page.count_chars() > 30
                    text_page.close()
                    if has_text:
                        page.close()
                        continue
                    bitmap = page.render(scale=1.25)
                    picture = bitmap.to_pil().convert("RGB")
                    angle, _elapsed = orientation(np.array(picture)[:, :, ::-1].copy())
                    angle = int(angle) if angle in ("90", "180", "270") else 0
                    upright = picture.rotate(angle, expand=True, fillcolor="white")
                    pixels = np.array(upright)
                    skew = get_angle(pixels, vertical_image_shape=900, angle_max=5)
                    # Agree with the central text area before changing an entire scanned page.
                    central = pixels[: max(1, int(len(pixels) * 0.75))]
                    confirmation = get_angle(central, vertical_image_shape=900, angle_max=5)
                    if not (0.35 <= abs(skew) <= 4.5 and abs(skew - confirmation) <= 0.4):
                        skew = 0.0
                    picture.close()
                    upright.close()
                    bitmap.close()
                    if not angle and not skew:
                        page.close()
                        continue
                    if corrected is None:
                        corrected = pdfium.PdfDocument.new()
                        corrected.import_pages(document)
                    corrections[number].update(rotation=angle, deskew=round(skew, 3))
                    if skew:
                        bitmap = page.render(scale=2.0)
                        picture = bitmap.to_pil().convert("RGB")
                        rotated = picture.rotate(
                            angle + skew,
                            resample=Image.Resampling.BICUBIC,
                            expand=True,
                            fillcolor="white",
                        )
                        corrections[number].update(
                            rotation_mapping(picture.size, angle + skew, rotated.size)
                        )
                        corrections[number]["normalized_size"] = [rotated.width / 2, rotated.height / 2]
                        new_bitmap = pdfium.PdfBitmap.from_pil(rotated)
                        corrected.del_page(number - 1)
                        new_page = corrected.new_page(
                            rotated.width / 2, rotated.height / 2, index=number - 1
                        )
                        image = pdfium.PdfImage.new(corrected)
                        image.set_bitmap(new_bitmap)
                        image.set_matrix(
                            pdfium.PdfMatrix(rotated.width / 2, 0, 0, rotated.height / 2, 0, 0)
                        )
                        new_page.insert_obj(image)
                        new_page.gen_content()
                        new_page.close()
                        new_bitmap.close()
                        rotated.close()
                        picture.close()
                        bitmap.close()
                    else:
                        copied = corrected[number - 1]
                        copied.set_rotation((copied.get_rotation() - angle) % 360)
                        corrections[number].update(rotation_mapping(source_size, angle))
                        corrections[number]["normalized_size"] = list(copied.get_size())
                        copied.close()
                    page.close()
                if corrected is None:
                    return source, corrections
                target = staging / "normalized.pdf"
                corrected.save(target)
            finally:
                if corrected is not None:
                    corrected.close()
        return target, corrections

    def fallback_image(self, record: dict, source: Path, staging: Path) -> str:
        existing = record.get("img_path", "")
        if existing and (staging / existing).is_file():
            return existing
        import pypdfium2 as pdfium

        with pdfium.PdfDocument(source) as document:
            page = document[record["page"] - 1]
            width, height = page.get_size()
            x0, y0, x1, y1 = record.get("bbox", [0, 0, 1, 1])
            if x1 <= x0 or y1 <= y0:
                x0, y0, x1, y1 = 0, 0, 1, 1
            bitmap = page.render(
                scale=2, crop=(x0 * width, (1 - y1) * height, (1 - x1) * width, y0 * height)
            )
            picture = bitmap.to_pil()
            relative = f"images/{record['id']}-fallback.png"
            (staging / "images").mkdir(exist_ok=True)
            picture.save(staging / relative)
            picture.close()
            bitmap.close()
            page.close()
        record["fallback_image"] = relative
        return relative

    def convert(self, source: Path, pages: list[int], settings: Settings, staging: Path) -> dict:
        started_at = timestamp()
        started = time.monotonic()
        source_stat = source.stat()
        with source.open("rb") as source_stream:
            source_sha256 = hashlib.file_digest(source_stream, "sha256").hexdigest()
        self.window_done = 0
        self.total = len(pages)
        engine = self.configure(settings)
        processing_source, rotations = self.normalize_orientation(source, pages, staging)
        markdown, blocks, page_records, warnings = MappedMarkdown(), [], [], []
        image_count = 0
        body_count = 0
        for run in page_runs(pages):
            self.phase("解析 PDF")
            result = engine(
                processing_source,
                start_page_id=run[0] - 1,
                end_page_id=run[-1] - 1,
                parse_method=settings.mode,
                f_dump_middle_json=False,
                f_dump_content_list=False,
                f_draw_layout_bbox=False,
                f_draw_span_bbox=False,
            )
            raw_pages = result.middle_json.get("pdf_info", [])
            if len(raw_pages) != len(run):
                raise ValueError("解析结果缺少页面，已停止发布。请缩小范围或重新导出 PDF。")
            mapping = {}
            content = result.content_list_json or []
            for index, record in enumerate(content):
                local_page = int(record.get("page_idx", 0))
                if not 0 <= local_page < len(run):
                    raise ValueError("引擎返回了无法映射的页面，已停止发布。")
                record["page"] = run[local_page]
                record.pop("page_idx", None)
                record["id"] = f"p{record['page']}-b{index + 1}"
                record["_run_start"] = run[0]
                if record.get("bbox"):
                    record["bbox"] = [
                        min(1, max(0, float(value) / 1000)) for value in record["bbox"]
                    ]
                if record.get("type") in ("text", "equation", "table"):
                    body_count += 1
            run_markdown = attach_engine_mapping(
                raw_pages, content, result.markdown, run, rotations, engine.make_md_mode
            )
            image_sources = {}
            normal_images = set()
            for record in content:

                def collect(value):
                    if isinstance(value, dict):
                        if value.get("img_path"):
                            image_sources[value["img_path"]] = record["page"]
                            if record.get("type") == "image":
                                normal_images.add(value["img_path"])
                        for item in value.values():
                            collect(item)
                    elif isinstance(value, list):
                        for item in value:
                            collect(item)

                collect(record)
            for filename, data in result.images.items():
                original = "images/" + filename.replace("\\", "/").removeprefix("images/")
                page_number = image_sources.get(original, run[0])
                if not settings.extract_images and original in normal_images:
                    run_markdown.replace(
                        f"![]({original})", f"[图片，原文第 {page_number} 页]"
                    )
                    mapping[original] = ""
                    continue
                image_count += 1
                suffix = Path(filename).suffix.lower()
                if suffix not in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"):
                    suffix = ".png"
                relative = f"images/page-{page_number:03d}-image-{image_count:03d}{suffix}"
                target = staging / relative
                target.parent.mkdir(exist_ok=True)
                target.write_bytes(data)
                mapping[original] = relative
                mapping[filename] = relative
            content = remap_object(content, mapping)
            run_markdown.remap_resources(mapping)
            for record in content:
                text = record.get("text", "")
                invalid = invalid_formulas(text) if settings.formula else []
                if invalid:
                    fallback = self.fallback_image(record, processing_source, staging)
                    for expression in invalid:
                        run_markdown.replace(
                            expression, f"[公式待核对：原文第 {record['page']} 页] ![]({fallback})"
                        )
                    warnings.append(
                        {
                            "code": "invalid_formula_syntax",
                            "page": record["page"],
                            "bbox": record.get("bbox"),
                            "block_ids": [record["id"]],
                            "source_regions": record.get("source_regions", []),
                            "image": fallback,
                            "message": "公式语法不完整，已保留区域图片，请对照原文核对。",
                        }
                    )
                if record.get("type") == "equation" and (
                    record.get("text_format") != "latex" or not text.strip()
                ):
                    fallback = self.fallback_image(record, processing_source, staging)
                    if fallback not in run_markdown.text:
                        run_markdown.append(
                            f"\n\n[公式待核对：原文第 {record['page']} 页] ![]({fallback})\n",
                            record["id"],
                        )
                    warnings.append(
                        {
                            "code": "formula_fallback",
                            "page": record["page"],
                            "bbox": record.get("bbox"),
                            "block_ids": [record["id"]],
                            "source_regions": record.get("source_regions", []),
                            "image": fallback,
                            "message": "公式未完整识别，请核对保留的区域图片。",
                        }
                    )
                if record.get("type") == "table" and not record.get("table_body"):
                    fallback = self.fallback_image(record, processing_source, staging)
                    if fallback not in run_markdown.text:
                        run_markdown.append(
                            f"\n\n[表格待核对：原文第 {record['page']} 页] ![]({fallback})\n",
                            record["id"],
                        )
                    warnings.append(
                        {
                            "code": "table_fallback",
                            "page": record["page"],
                            "bbox": record.get("bbox"),
                            "block_ids": [record["id"]],
                            "source_regions": record.get("source_regions", []),
                            "image": fallback,
                            "message": "表格未完整恢复结构，请核对保留的区域图片。",
                        }
                    )
            for local_index, page in enumerate(raw_pages):
                number = run[local_index]
                evidence, scores = ocr_evidence(page, number, source_blocks=True)
                for warning in evidence:
                    warning["_run_start"] = run[0]
                warnings.extend(evidence)
                page_records.append(
                    {
                        "page": number,
                        "size": page.get("page_size"),
                        "reading_method": page.get("_reading_method", "unknown"),
                        "ocr_confidence_min": min(scores) if scores else None,
                        "rotation_correction": rotations.get(number, {}).get("rotation", 0),
                        "deskew_correction": rotations.get(number, {}).get("deskew", 0),
                        **{key: value for key, value in rotations.get(number, {}).items()
                           if key not in ("rotation", "deskew")},
                    }
                )
                page_blocks = [block for block in content if block["page"] == number]
                if not page_blocks:
                    warnings.append(
                        {
                            "code": "empty_page_result",
                            "page": number,
                            "_anchor_id": f"empty-p{number}",
                            "message": "该页没有识别到内容，可能为空白页，请核对原文。",
                        }
                    )
                    run_markdown.append(
                        f"\n\n[原文第 {number} 页未识别到内容，请核对是否为空白。]\n",
                        f"empty-p{number}",
                    )
            markdown.extend(run_markdown)
            blocks.extend(content)
            del result
        if not body_count and not image_count:
            raise ValueError(
                "所选页面没有有效识别结果，未生成空的 Markdown。请检查扫描清晰度或改用强制 OCR。"
            )
        self.phase("整理与校验输出")
        warnings.extend(continuation_warnings(blocks))
        markdown.finish(settings.table_format)
        finalize_mapping(markdown, blocks, warnings, page_records, rotations)
        (staging / "document.md").write_text(markdown.text, encoding="utf-8")
        markdown_sha256 = hashlib.sha256((staging / "document.md").read_bytes()).hexdigest()
        current_stat = source.stat()
        if (source_stat.st_size, source_stat.st_mtime_ns) != (current_stat.st_size, current_stat.st_mtime_ns):
            raise ValueError("转换期间原 PDF 已发生变化，请重新添加并转换。")
        normalized = staging / "normalized.pdf"
        if normalized.exists():
            normalized.unlink()
        document = {
            "schema_version": "1.1",
            "offset_unit": "utf16",
            "markdown_sha256": markdown_sha256,
            "source": {"name": source.name, "selected_pages": pages,
                       "sha256": source_sha256, "size_bytes": source_stat.st_size},
            "pages": page_records,
            "blocks": blocks,
            "warnings": warnings,
        }
        json_write(staging / "content_list.json", document)
        image_count = sum(path.is_file() for path in (staging / "images").glob("*"))
        report = {
            "schema_version": "1.1",
            "markdown_sha256": markdown_sha256,
            "source_sha256": source_sha256,
            "tool_version": __version__,
            "engine": "rapid-doc==0.9.11",
            "source_name": source.name,
            "selected_pages": pages,
            "settings": settings.model_dump(),
            "started_at": started_at,
            "finished_at": timestamp(),
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "model_profile": self.info["manifest"]["profile"],
            "warnings": warnings,
            "platform": platform.platform(),
            "python_version": platform.python_version(),
            "architecture": platform.machine(),
            "native_pages": sum(p["reading_method"] == "native" for p in page_records),
            "ocr_pages": sum(p["reading_method"] == "ocr" for p in page_records),
            "formula_fallback_count": sum(
                w["code"] in ("formula_fallback", "invalid_formula_syntax") for w in warnings
            ),
            "table_fallback_count": sum(w["code"] == "table_fallback" for w in warnings),
            "models": [
                {"id": f["id"], "sha256": f["sha256"]} for f in self.info["manifest"]["files"]
            ],
            "image_count": image_count,
            "table_count": sum(b.get("type") == "table" for b in blocks),
            "formula_count": len(
                re.findall(r"\$\$[\s\S]*?\$\$|(?<!\\)\$(?!\$)(?:\\.|[^$])+\$", markdown.text)
            ),
        }
        json_write(staging / "conversion-report.json", report)
        return report
