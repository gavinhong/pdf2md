from pathlib import Path

import pytest

from pdf2md.output import clean_staging, publish, remap_object, remap_resources, table_markup


def stage(root: Path, identifier: str):
    path = root / ".pdf2md-tmp" / identifier
    path.mkdir(parents=True)
    (path / "document.md").write_text("# Test", encoding="utf-8")
    return path


def test_publish_never_overwrites_and_cancel_never_publishes(tmp_path):
    original = publish(stage(tmp_path, "first"), tmp_path, "原文")
    second = publish(stage(tmp_path, "second"), tmp_path, "原文")
    assert second.name == "原文_2"
    assert (original / "原文.md").read_text("utf-8") == "# Test"
    cancelled = stage(tmp_path, "cancelled")
    with pytest.raises(InterruptedError):
        publish(cancelled, tmp_path, "原文", lambda: True)
    clean_staging(cancelled, tmp_path)
    assert not cancelled.exists()
    clean_staging(original, tmp_path)
    assert original.exists()


def test_table_semantics_and_resource_references():
    simple = "<table><tr><th>Name</th><th>Score</th></tr><tr><td>A</td><td>95</td></tr></table>"
    assert "| Name | Score |" in table_markup(simple, "auto")
    merged = '<table><tr><td colspan="2">A</td></tr></table>'
    assert table_markup(merged, "auto") == merged
    math = "<table><tr><td>$x^2$</td></tr></table>"
    assert table_markup(math, "auto") == math
    text = 'keep images/a.png as text; ![](images/a.png) <img src="images/a.png">'
    mapped = remap_resources(text, {"images/a.png": "images/page-002-image-001.png"})
    assert "keep images/a.png as text" in mapped
    assert mapped.count("page-002-image-001") == 2
    assert remap_object({"img_path": "images/a.png"}, {"images/a.png": "new"}) == {
        "img_path": "new"
    }
