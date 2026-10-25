from copy import deepcopy
from types import SimpleNamespace

import pytest
from PIL import Image

from pdf2md.output import remap_resources, table_markup
from pdf2md.quality import ocr_evidence
from pdf2md.review_mapping import (
    MappedMarkdown,
    apply_affine,
    attach_engine_mapping,
    collect_sources,
    finalize_mapping,
    install_source_hooks,
    mark_sources,
    rotation_mapping,
    source_region,
    utf16_length,
)


def text_block(text, bbox=(10, 10, 90, 30), kind="text", score=0.99):
    return {"type": kind, "bbox": list(bbox), "lines": [
        {"bbox": list(bbox), "spans": [
            {"type": "text", "content": text, "bbox": list(bbox), "score": score}
        ]}
    ]}


def table_block(value, bbox):
    html = ("<table><tbody><tr><th>Name</th><th>Value</th></tr>"
            f"<tr><td>{value}</td><td>12</td></tr></tbody></table>")
    return {"type": "table", "bbox": list(bbox), "blocks": [
        {"type": "table_body", "bbox": list(bbox), "lines": [{"spans": [
            {"type": "table", "bbox": list(bbox), "html": html}
        ]}]}
    ]}


def page(blocks, index=0):
    return {"page_idx": index, "page_size": [100, 200],
            "preproc_blocks": blocks, "discarded_blocks": []}


def emit(pages, physical_pages):
    from rapid_doc.backend.pipeline.pipeline_middle_json_mkcontent import union_make
    from rapid_doc.utils.enum_class import MakeMode

    markdown = union_make(pages, MakeMode.MM_MD, "images")
    content = union_make(pages, MakeMode.CONTENT_LIST, "images")
    for index, block in enumerate(content):
        block["page"] = physical_pages[block.pop("page_idx")]
        block["id"] = f"p{block['page']}-b{index + 1}"
        block["_run_start"] = physical_pages[0]
        block["bbox"] = [value / 1000 for value in block["bbox"]]
    return markdown, content


@pytest.mark.parametrize("angle", [0, 90, 180, 270, 2.12345, -3.237, 92.12345, 272.34])
def test_affine_matches_actual_pillow_expansion_and_inverse(angle):
    # Odd raster dimensions exercise expansion rounding and center offsets.
    original = Image.new("RGB", (99, 62))
    original.putdata([(x * 2 % 256, y * 4 % 256, (x * y) % 256)
                      for y in range(62) for x in range(99)])
    rotated = original.rotate(angle, expand=True)
    mapping = rotation_mapping(original.size, angle, rotated.size)
    reverse = mapping["normalized_to_source"]
    width, height = original.size
    out_width, out_height = rotated.size
    pixels = [reverse[0] * width / out_width, reverse[1] * width / out_height,
              reverse[2] * width, reverse[3] * height / out_width,
              reverse[4] * height / out_height, reverse[5] * height]
    reconstructed = original.transform(rotated.size, Image.Transform.AFFINE, pixels)
    assert reconstructed.tobytes() == rotated.tobytes()
    point = [0.23, 0.76]
    assert apply_affine(reverse, apply_affine(mapping["source_to_normalized"], point)) == pytest.approx(point)


def test_inverse_region_is_source_polygon_and_clipped_to_page():
    mapping = rotation_mapping((100, 200), 90)
    region = source_region(7, [0.1, 0.2, 0.4, 0.6], {7: mapping})
    assert region["page"] == 7
    assert region["bbox"] == pytest.approx([0.4, 0.1, 0.8, 0.4])
    assert len(region["polygon"]) == 4
    assert source_region(7, [0, 0, 0, 1], {}) is None
    skew = rotation_mapping((100, 200), 3.12)
    clipped = source_region(1, [0, 0, 1, 1], {1: skew})
    assert all(0 <= value <= 1 for point in clipped["polygon"] for value in point)
    assert clipped["bbox"] == [0, 0, 1, 1]


def test_export_rewrites_keep_repeated_and_utf16_ranges_exact():
    table = "<table><tr><th>A</th></tr><tr><td>😀</td></tr></table>"
    text = "\n 𠮷😀 é ![](images/a.png)\n\nrepeat\n\nrepeat\n\n" + table + "  \n"
    mapped = MappedMarkdown(text)
    start = text.index("repeat")
    mapped.anchors["first"] = [[start, start + 6]]
    mapped.anchors["second"] = [[start + 8, start + 14]]
    mapped.anchors["table"] = [[text.index("<table>"), text.index("</table>") + 8]]
    mapped.remap_resources({"images/a.png": "images/long-renamed.png"})
    mapped.replace("repeat", "修正😀")
    mapped.finish("auto")
    expected = table_markup(remap_resources(text, {"images/a.png": "images/long-renamed.png"})
                            .replace("repeat", "修正😀").strip() + "\n", "auto")
    assert mapped.text == expected
    ranges = mapped.utf16_ranges()
    encoded = mapped.text.encode("utf-16-le")
    for key in ("first", "second"):
        a, b = ranges[key][0]
        assert encoded[a * 2:b * 2].decode("utf-16-le") == "修正😀"
    assert ranges["first"] != ranges["second"]
    assert utf16_length("𠮷😀é") == 6


def test_final_block_emitter_preserves_columns_formulas_and_duplicates():
    # The supplied final order is the upstream reading order, including two
    # columns. Do not geometrically sort by y/x while attaching anchors.
    final = [page([])]
    final[0]["para_blocks"] = [text_block("right column first", (60, 10, 95, 20)),
                                text_block("left column next", (5, 5, 45, 20)),
                                text_block("repeat 😀"), text_block("repeat 😀"),
                                {"type": "interline_equation", "bbox": [5, 30, 80, 50],
                                 "lines": [{"spans": [{"type": "interline_equation",
                                                        "content": r"x^2\tag{2}"}]}]}]
    markdown, content = emit(final, [3])
    mapped = attach_engine_mapping(final, content, markdown, [3], {}, "mm_markdown")
    assert mapped.text == markdown
    assert len(mapped.anchors) == 5
    for block in content:
        assert block["source_regions"][0]["page"] == 3
    assert mapped.anchors[content[2]["id"]] != mapped.anchors[content[3]["id"]]
    assert "right column first" in markdown[:markdown.index("left column next")]
    assert r"\tag{2}" in markdown


def test_mismatch_keeps_upstream_text_without_inventing_ranges():
    final = [page([])]
    final[0]["para_blocks"] = [text_block("same")]
    markdown, content = emit(final, [1])
    mapped = attach_engine_mapping(final, content, markdown + " upstream extra", [1], {}, "mm_markdown")
    assert mapped.text.endswith(" upstream extra")
    assert not mapped.anchors


def test_paragraph_provenance_and_ocr_uses_actual_source_page():
    from rapid_doc.backend.pipeline.para_split import para_split

    pages = [page([text_block("previous paragraph", (10, 150, 90, 170))], 0),
             page([text_block("continuing paragraph", (10, 10, 90, 30), score=0.6)], 1)]
    mark_sources(pages)
    # Simulate the exact upstream cross-page lines.extend operation; provenance
    # must survive deepcopy and source page containers must remain unchanged.
    pages[0]["para_blocks"] = deepcopy(pages[0]["preproc_blocks"])
    pages[1]["para_blocks"] = deepcopy(pages[1]["preproc_blocks"])
    pages[0]["para_blocks"][0]["lines"].extend(pages[1]["para_blocks"][0]["lines"])
    pages[1]["para_blocks"][0]["lines"] = []
    markdown, content = emit(pages, [3, 4])
    mapped = attach_engine_mapping(pages, content, markdown, [3, 4], {}, "mm_markdown")
    warnings, _ = ocr_evidence(pages[1], 4, source_blocks=True)
    warnings[0]["_run_start"] = 3
    mapped.finish("html")
    finalize_mapping(mapped, content, warnings, [], {})
    assert warnings[0]["page"] == 4
    assert warnings[0]["block_ids"] == [content[0]["id"]]
    assert {region["page"] for region in content[0]["source_regions"]} == {3, 4}
    assert warnings[0]["markdown_ranges"] == content[0]["markdown_ranges"]
    # Also check that the unmodified real splitter deep-copies our source keys.
    fresh = [page([text_block("short sentence.")])]
    mark_sources(fresh)
    para_split(fresh)
    assert collect_sources(fresh[0]["para_blocks"][0])[0]["page_idx"] == 0


def test_table_hooks_preserve_identical_merge_and_three_page_provenance():
    from rapid_doc.backend.pipeline import model_json_to_middle_json as module
    from rapid_doc.utils.table_merge import merge_table

    original = [page([table_block(str(i + 1), [10, 10, 90, 190])], i) for i in range(3)]
    baseline = deepcopy(original)
    # Upstream split/merge before installing wrappers supplies the reference.
    module.para_split(baseline)
    merge_table(baseline)
    expected, _ = emit(baseline, [2, 3, 4])
    install_source_hooks()
    actual = deepcopy(original)
    module.para_split(actual)
    merge_table(actual)
    markdown, content = emit(actual, [2, 3, 4])
    assert markdown == expected
    assert markdown.count("<table>") == 1
    mapped = attach_engine_mapping(actual, content, markdown, [2, 3, 4], {}, "mm_markdown")
    assert {region["page"] for region in content[0]["source_regions"]} == {2, 3, 4}
    assert mapped.anchors[content[0]["id"]]
    assert "<td>3</td>" in markdown


def test_mock_conversion_records_hashes_anchors_and_stable_warning_ids(tmp_path):
    import hashlib
    import json

    from rapid_doc.backend.pipeline.para_split import para_split
    from rapid_doc.backend.pipeline.pipeline_middle_json_mkcontent import union_make
    from rapid_doc.utils.enum_class import MakeMode

    from pdf2md.domain import Settings
    from pdf2md.engine import RapidDocAdapter

    source = tmp_path / "original.pdf"
    source.write_bytes(b"test-source-bytes-without-model-inference")
    pages = [page([text_block("𠮷😀 uncertain OCR", score=0.7)])]
    mark_sources(pages)
    para_split(pages)
    expected = union_make(pages, MakeMode.MM_MD, "images")

    class FakeEngine:
        make_md_mode = "mm_markdown"

        def __call__(self, *_args, **_kwargs):
            raw = deepcopy(pages)
            return SimpleNamespace(markdown=expected, images={}, middle_json={"pdf_info": raw},
                                   content_list_json=union_make(raw, MakeMode.CONTENT_LIST, "images"))

    adapter = RapidDocAdapter.__new__(RapidDocAdapter)
    adapter.info = {"manifest": {"profile": "test", "files": []}}
    adapter.configure = lambda _settings: FakeEngine()
    adapter.normalize_orientation = lambda *_args: (source, {})
    adapter.phase = lambda _phase: None
    warning_ids = []
    for index in (1, 2):
        staging = tmp_path / str(index)
        staging.mkdir()
        adapter.convert(source, [1], Settings(), staging)
        document = json.loads((staging / "content_list.json").read_text("utf-8"))
        md = staging / "document.md"
        assert md.read_text("utf-8") == expected.strip() + "\n"
        assert document["source"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
        assert document["markdown_sha256"] == hashlib.sha256(md.read_bytes()).hexdigest()
        warning = document["warnings"][0]
        assert warning["block_ids"] == [document["blocks"][0]["id"]]
        assert warning["source_regions"][0]["polygon"]
        assert warning["markdown_ranges"][0][1] == utf16_length(expected)
        assert not any(key.startswith("_") for key in warning)
        warning_ids.append(warning["id"])
    assert warning_ids[0] == warning_ids[1]
