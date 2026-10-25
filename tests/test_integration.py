import json
from pathlib import Path

import pypdfium2 as pdfium
import pytest

from pdf2md.engine import RapidDocAdapter

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.integration
def test_real_mixed_noncontiguous_pages_and_image_switch(service, tmp_path):
    native = next(ROOT.glob("Reddy*.pdf"))
    source = tmp_path / "mixed.pdf"
    with pdfium.PdfDocument.new() as document:
        with pdfium.PdfDocument(native) as original:
            document.import_pages(original, pages=[0])
        blank = document.new_page(595, 842)
        blank.close()
        with pdfium.PdfDocument(ROOT / "tests/fixtures/scanned-math.pdf") as scan:
            document.import_pages(scan)
        rotated = document[2]
        rotated.set_rotation(90)
        rotated.close()
        document.save(source)
    service.import_files([str(source)])
    assert service.start(
        {"output_dir": str(tmp_path / "out"), "page_range": "1,3", "extract_images": False}
    )["ok"]
    service.batch_thread.join(300)
    assert not service.busy
    task = service.tasks[0]
    assert task.status.startswith("succeeded"), task.error
    markdown = Path(task.markdown_file).read_text("utf-8")
    assert "Deep Learning Approach" in markdown
    assert "反比例函数" in markdown
    assert "原文第 3 页" in markdown and "![]" not in markdown
    report = json.loads((Path(task.output_dir) / "conversion-report.json").read_text("utf-8"))
    assert (report["native_pages"], report["ocr_pages"]) == (1, 1)
    assert report["source_total_pages"] == 3
    assert report["selected_pages"] == [1, 3]
    assert report["formula_count"] >= 1
    assert report["task_id"] == task.id
    content = json.loads((Path(task.output_dir) / "content_list.json").read_text("utf-8"))
    assert [p["page"] for p in content["pages"]] == [1, 3]
    assert content["pages"][1]["rotation_correction"] in (90, 270)
    assert all(b["page"] in (1, 3) for b in content["blocks"])


@pytest.mark.integration
def test_real_light_skew_normalization(tmp_path):
    import numpy as np
    from jdeskew.estimator import get_angle
    from PIL import Image

    source = tmp_path / "skewed.pdf"
    with pdfium.PdfDocument(ROOT / "tests/fixtures/scanned-math.pdf") as document:
        page = document[0]
        bitmap = page.render(scale=2)
        picture = bitmap.to_pil().convert("RGB")
        skewed = picture.rotate(
            2, resample=Image.Resampling.BICUBIC, expand=True, fillcolor="white"
        )
        skewed.save(source, "PDF", resolution=144)
        skewed.close()
        picture.close()
        bitmap.close()
        page.close()
    adapter = RapidDocAdapter(ROOT / "models", lambda message: None)
    result, corrections = adapter.normalize_orientation(source, [1], tmp_path)
    assert result != source
    assert -2.5 < corrections[1]["deskew"] < -1.5
    with pdfium.PdfDocument(result) as document:
        page = document[0]
        bitmap = page.render(scale=1)
        picture = bitmap.to_pil().convert("RGB")
        assert abs(get_angle(np.array(picture), vertical_image_shape=900, angle_max=5)) < 0.4
        picture.close()
        bitmap.close()
        page.close()
