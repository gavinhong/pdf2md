"""Review contracts exercise real files and UTF-16 offsets without loading models."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from pdf2md.domain import Task
from pdf2md.service import ApplicationService


def utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def review_result(service, pdf_path, tmp_path):
    folder = tmp_path / "核对结果"
    folder.mkdir()
    first, second = "第一段😀：原始文字。", "第二段：保留内容。"
    markdown = f"{first}\n\n{second}\n"
    md = folder / "测试.md"
    md.write_text(markdown, encoding="utf-8")
    ranges = [[0, utf16(first)], [utf16(first + "\n\n"), utf16(markdown.rstrip())]]
    regions = [
        {"page": 1, "polygon": [[0.1, 0.1], [0.8, 0.1], [0.8, 0.2], [0.1, 0.2]]},
        {"page": 2, "polygon": [[0.1, 0.3], [0.8, 0.3], [0.8, 0.4], [0.1, 0.4]]},
    ]
    blocks = [
        {
            "id": f"b{index + 1}",
            "type": "text",
            "page": index + 1,
            "text": text,
            "markdown_ranges": [ranges[index]],
            "source_regions": [regions[index]],
        }
        for index, text in enumerate((first, second))
    ]
    warnings = [
        {
            "id": warning_id,
            "code": "low_ocr_confidence",
            "page": index + 1,
            "message": "请核对该区域文字。",
            "block_ids": [f"b{index + 1}"],
            "markdown_ranges": [ranges[index]],
            "source_regions": [regions[index]],
        }
        for warning_id, index in (("w1", 0), ("w1b", 0), ("w2", 1))
    ]
    source = {
        "name": pdf_path.name,
        "path": str(pdf_path),
        "sha256": hashlib.sha256(pdf_path.read_bytes()).hexdigest(),
        "selected_pages": [1, 2],
    }
    content = {
        "schema_version": "1.1",
        "source": source,
        "pages": [{"page": page, "width": 595, "height": 842} for page in (1, 2)],
        "blocks": blocks,
        "warnings": warnings,
    }
    write_json(folder / "content_list.json", content)
    write_json(
        folder / "conversion-report.json",
        {"schema_version": "1.1", "source_name": pdf_path.name, "warnings": warnings},
    )
    task = Task(
        id="review-test",
        name=pdf_path.name,
        source=str(pdf_path),
        pages=3,
        status="succeeded_with_warnings",
        output_dir=str(folder),
        markdown_file=str(md),
        warnings=warnings,
    )
    service.tasks.append(task)
    return {"task": task, "markdown": markdown, "md": md, "folder": folder, "content": content}


def document(service, result):
    reply = service.review_document(result["task"].id)
    assert reply["ok"], reply
    return reply


def save(service, result, doc, *, markdown=None, changes=None, updates=None):
    return service.save_review(
        result["task"].id,
        {
            "markdown": doc["markdown"] if markdown is None else markdown,
            "expected_sha256": doc["sha256"],
            "expected_revision": doc["review_revision"],
            "changes": [] if changes is None else changes,
            "review_updates": {} if updates is None else updates,
        },
    )


def flags(doc):
    return {item["id"]: item["reviewed"] for item in doc["warnings"]}


def test_review_returns_complete_document_and_original_regions(service, review_result):
    doc = document(service, review_result)
    assert doc["markdown"] == review_result["markdown"]
    assert doc["sha256"] == hashlib.sha256(review_result["md"].read_bytes()).hexdigest()
    assert doc["pending_count"] == 3 and doc["reviewed_count"] == 0
    assert doc["source"]["available"] and not doc["source"]["changed"]
    assert doc["warnings"][0]["block_ids"] == ["b1"]
    assert doc["warnings"][0]["markdown_ranges"] == [[0, utf16("第一段😀：原始文字。")]]
    assert doc["warnings"][0]["source_regions"][0]["page"] == 1


def test_mark_all_reviewed_and_undo_updates_task_without_erasing_warnings(service, review_result):
    doc = document(service, review_result)
    marked = save(service, review_result, doc, updates={key: True for key in flags(doc)})
    assert marked["ok"] and marked["pending_count"] == 0
    assert review_result["task"].status == "succeeded_reviewed"
    assert len(review_result["task"].warnings) == 3
    report = json.loads((review_result["folder"] / "conversion-report.json").read_text("utf-8"))
    assert len(report["warnings"]) == 3
    undone = save(service, review_result, marked, updates={"w1": False})
    assert undone["ok"] and undone["pending_count"] == 1
    assert review_result["task"].status == "succeeded_with_warnings"


def test_edit_resets_only_related_block_and_maps_utf16_ranges(service, review_result):
    doc = document(service, review_result)
    marked = save(service, review_result, doc, updates={key: True for key in flags(doc)})
    old, new = "原始", "修订后的"
    prefix = doc["markdown"].split(old, 1)[0]
    start = utf16(prefix)
    changed = save(
        service,
        review_result,
        marked,
        markdown=doc["markdown"].replace(old, new),
        changes=[{"from": start, "to": start + utf16(old), "insert": new}],
    )
    assert changed["ok"], changed
    assert flags(changed) == {"w1": False, "w1b": False, "w2": True}
    old_second = doc["blocks"][1]["markdown_ranges"][0]
    new_second = changed["blocks"][1]["markdown_ranges"][0]
    assert new_second == [offset + utf16(new) - utf16(old) for offset in old_second]
    assert review_result["md"].read_text("utf-8") == changed["markdown"]


def test_save_and_mark_applies_explicit_review_after_edit_reset(service, review_result):
    doc = document(service, review_result)
    marked = save(service, review_result, doc, updates={key: True for key in flags(doc)})
    changed = save(
        service,
        review_result,
        marked,
        markdown="新" + doc["markdown"][1:],
        changes=[{"from": 0, "to": 1, "insert": "新"}],
        updates={"w1": True},
    )
    assert changed["ok"]
    assert flags(changed) == {"w1": True, "w1b": False, "w2": True}


def test_delete_entire_block_preserves_pending_warning(service, review_result):
    doc = document(service, review_result)
    marked = save(service, review_result, doc, updates={key: True for key in flags(doc)})
    first = doc["markdown"].split("\n\n")[0]
    reply = save(
        service,
        review_result,
        marked,
        markdown=doc["markdown"][len(first):],
        changes=[{"from": 0, "to": utf16(first), "insert": ""}],
    )
    assert reply["ok"]
    assert flags(reply) == {"w1": False, "w1b": False, "w2": True}


def test_unrelated_trailing_edit_keeps_reviewed_flags(service, review_result):
    doc = document(service, review_result)
    marked = save(service, review_result, doc, updates={key: True for key in flags(doc)})
    end = utf16(doc["markdown"])
    reply = save(
        service,
        review_result,
        marked,
        markdown=doc["markdown"] + "\n补充笔记",
        changes=[{"from": end, "to": end, "insert": "\n补充笔记"}],
    )
    assert reply["ok"] and all(flags(reply).values())
    assert review_result["task"].status == "succeeded_reviewed"


def test_multiple_changes_use_same_original_utf16_document(service, review_result):
    doc = document(service, review_result)
    first = utf16(doc["markdown"].split("😀")[0])
    second = utf16(doc["markdown"].split("保留")[0])
    new_text = doc["markdown"].replace("😀", "𠮷😀").replace("保留", "已修正")
    reply = save(
        service,
        review_result,
        doc,
        markdown=new_text,
        changes=[
            {"from": first, "to": first + 2, "insert": "𠮷😀"},
            {"from": second, "to": second + 2, "insert": "已修正"},
        ],
    )
    assert reply["ok"] and reply["markdown"] == new_text
    assert review_result["md"].read_text("utf-8") == new_text


def test_first_manual_edit_keeps_immutable_original_backup(service, review_result):
    original = review_result["md"].read_bytes()
    doc = document(service, review_result)
    edited = save(
        service,
        review_result,
        doc,
        markdown="一" + doc["markdown"],
        changes=[{"from": 0, "to": 0, "insert": "一"}],
    )
    assert edited["ok"]
    backup = review_result["md"].with_name(review_result["md"].stem + ".original.md")
    assert backup.read_bytes() == original
    edited_again = save(
        service,
        review_result,
        edited,
        markdown="二" + edited["markdown"],
        changes=[{"from": 0, "to": 0, "insert": "二"}],
    )
    assert edited_again["ok"] and backup.read_bytes() == original


def test_review_progress_and_task_restore_after_restart(service, review_result):
    doc = document(service, review_result)
    assert save(service, review_result, doc, updates={"w1": True})["ok"]
    assert (review_result["folder"] / "review-state.json").is_file()
    restored = ApplicationService(settings_path=service.settings_path)
    try:
        task = next(item for item in restored.tasks if item.id == review_result["task"].id)
        reply = restored.review_document(task.id)
        assert reply["ok"] and flags(reply)["w1"]
        assert reply["pending_count"] == 2
        assert task.status == "succeeded_with_warnings"
    finally:
        restored.shutdown()


def test_external_markdown_change_returns_conflict_and_keeps_file(service, review_result):
    doc = document(service, review_result)
    review_result["md"].write_text("外部编辑器已修改\n", encoding="utf-8")
    reply = save(service, review_result, doc, updates={"w1": True})
    assert not reply["ok"] and reply["conflict"]
    assert review_result["md"].read_text("utf-8") == "外部编辑器已修改\n"


def test_stale_review_revision_is_rejected(service, review_result):
    doc = document(service, review_result)
    assert save(service, review_result, doc, updates={"w1": True})["ok"]
    stale = save(service, review_result, doc, updates={"w2": True})
    assert not stale["ok"] and stale["conflict"]
    assert flags(document(service, review_result)) == {"w1": True, "w1b": False, "w2": False}


def test_external_reload_resets_previous_review_flags(service, review_result):
    doc = document(service, review_result)
    assert save(service, review_result, doc, updates={key: True for key in flags(doc)})["ok"]
    review_result["md"].write_text("外部改写后的正文\n", encoding="utf-8")
    reloaded = document(service, review_result)
    assert not any(flags(reloaded).values())
    assert reloaded["notice"]
    assert review_result["task"].status == "succeeded_with_warnings"


def test_missing_source_still_allows_edit_and_review(service, review_result):
    Path(review_result["task"].source).unlink()
    doc = document(service, review_result)
    assert not doc["source"]["available"]
    reply = save(service, review_result, doc, updates={"w1": True})
    assert reply["ok"] and flags(reply)["w1"]


def test_changed_source_disables_original_region_confidence(service, review_result, pdf_path):
    pdf_path.write_bytes(pdf_path.read_bytes() + b"\n% changed source\n")
    doc = document(service, review_result)
    assert doc["source"]["available"] and doc["source"]["changed"]


def test_relink_accepts_only_original_pdf_fingerprint(service, review_result, pdf_path, tmp_path):
    original_bytes = pdf_path.read_bytes()
    pdf_path.unlink()
    different = tmp_path / "different.pdf"
    different.write_bytes(original_bytes + b"\n% different PDF\n")
    wrong = service.locate_review_source_path(review_result["task"].id, str(different))
    assert not wrong["ok"]
    moved = tmp_path / "moved-original.pdf"
    moved.write_bytes(original_bytes)
    reply = service.locate_review_source_path(review_result["task"].id, str(moved))
    assert reply["ok"] and reply["source"]["available"]
    assert not reply["source"]["changed"]
    assert Path(review_result["task"].source) == moved


def test_legacy_result_opens_without_invented_text_ranges(service, review_result):
    content = review_result["content"]
    content["schema_version"] = "1.0"
    for block in content["blocks"]:
        block.pop("markdown_ranges")
        block.pop("source_regions")
    for warning in content["warnings"]:
        for key in ("id", "block_ids", "markdown_ranges", "source_regions"):
            warning.pop(key)
    write_json(review_result["folder"] / "content_list.json", content)
    reply = service.open_review_result_path(str(review_result["md"]))
    assert reply["ok"], reply
    task = next(item for item in service.tasks if item.markdown_file == str(review_result["md"]))
    doc = service.review_document(task.id)
    assert doc["ok"] and len(doc["warnings"]) == 3
    assert all(not warning.get("markdown_ranges") for warning in doc["warnings"])
    assert len({warning["id"] for warning in doc["warnings"]}) == 3


def test_save_large_utf8_document_preserves_entire_content(service, review_result):
    doc = document(service, review_result)
    suffix = "\n\n" + "汉字😀" * 180_000
    text = doc["markdown"] + suffix
    assert len(text.encode("utf-8")) > 1024 * 1024
    reply = save(
        service,
        review_result,
        doc,
        markdown=text,
        changes=[{"from": utf16(doc["markdown"]), "to": utf16(doc["markdown"]), "insert": suffix}],
    )
    assert reply["ok"], reply
    assert review_result["md"].read_text("utf-8") == text
    assert reply["markdown"] == text


@pytest.mark.parametrize(
    "changes,markdown",
    [
        ([], "未声明的修改"),
        ([{"from": -1, "to": 0, "insert": "x"}], "x"),
        ([{"from": 0, "to": 9999, "insert": "x"}], "x"),
        ([{"from": 0, "to": 2, "insert": "x"}, {"from": 1, "to": 3, "insert": "y"}], "xy"),
        # Offset 4 splits the surrogate pair of the emoji after 第一段.
        ([{"from": 4, "to": 4, "insert": "x"}], "x"),
    ],
)
def test_invalid_change_sets_do_not_overwrite_markdown(service, review_result, changes, markdown):
    doc = document(service, review_result)
    reply = save(service, review_result, doc, markdown=markdown, changes=changes)
    assert not reply["ok"]
    assert review_result["md"].read_text("utf-8") == doc["markdown"]


def test_unknown_review_item_is_rejected(service, review_result):
    doc = document(service, review_result)
    reply = save(service, review_result, doc, updates={"not-an-existing-warning": True})
    assert not reply["ok"]
    assert review_result["md"].read_text("utf-8") == doc["markdown"]


def test_markdown_write_failure_keeps_file_and_review_status(service, review_result, monkeypatch):
    import pdf2md.review as review

    doc = document(service, review_result)
    original = review.atomic_write

    def fail_markdown(path, *args, **kwargs):
        if Path(path).suffix == ".md":
            raise OSError("模拟磁盘写入失败")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(review, "atomic_write", fail_markdown)
    reply = save(
        service,
        review_result,
        doc,
        markdown="新" + doc["markdown"],
        changes=[{"from": 0, "to": 0, "insert": "新"}],
        updates={key: True for key in flags(doc)},
    )
    assert not reply["ok"] and not reply.get("markdown_saved", False)
    assert review_result["md"].read_text("utf-8") == doc["markdown"]
    assert review_result["task"].status == "succeeded_with_warnings"


def test_state_write_failure_reports_saved_markdown_and_can_recover(service, review_result, monkeypatch):
    import pdf2md.review as review

    doc = document(service, review_result)
    original = review.atomic_write

    def fail_state(path, *args, **kwargs):
        if Path(path).name == "review-state.json":
            raise OSError("模拟核对进度写入失败")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(review, "atomic_write", fail_state)
    changed = "新" + doc["markdown"]
    reply = save(
        service,
        review_result,
        doc,
        markdown=changed,
        changes=[{"from": 0, "to": 0, "insert": "新"}],
        updates={key: True for key in flags(doc)},
    )
    assert not reply["ok"] and reply["markdown_saved"]
    assert review_result["md"].read_text("utf-8") == changed
    assert review_result["task"].status == "succeeded_with_warnings"
    monkeypatch.setattr(review, "atomic_write", original)
    recovered = document(service, review_result)
    assert recovered["markdown"] == changed and recovered["pending_count"] == 3


def test_unregistered_task_cannot_read_review_files(service):
    reply = service.review_document("unregistered-task")
    assert not reply["ok"]


@pytest.mark.parametrize("damage", ["null-warnings", "empty-warning", "block-without-ranges"])
def test_corrupt_review_state_restores_pending_without_losing_task(service, review_result, damage):
    doc = document(service, review_result)
    marked = save(service, review_result, doc, updates={key: True for key in flags(doc)})
    assert marked["ok"] and marked["pending_count"] == 0
    state_path = review_result["folder"] / "review-state.json"
    state = json.loads(state_path.read_text("utf-8"))
    if damage == "null-warnings":
        state["warnings"] = None
    elif damage == "empty-warning":
        state["warnings"] = [{}]
    else:
        state["blocks"] = [{"id": "b1"}]
    write_json(state_path, state)
    restored = ApplicationService(settings_path=service.settings_path)
    try:
        task = next(item for item in restored.tasks if item.id == review_result["task"].id)
        reply = restored.review_document(task.id)
        assert reply["ok"] and reply["pending_count"] == 3
        assert reply["reviewed_count"] == 0 and not any(flags(reply).values())
        assert reply["markdown"] == review_result["markdown"]
        assert "损坏" in reply["notice"]
        assert task.status == "succeeded_with_warnings"
    finally:
        restored.shutdown()


def test_null_task_index_does_not_crash_restart(service):
    write_json(service.settings_path.with_name("tasks.json"), {"schema_version": "1.0", "tasks": None})
    restored = ApplicationService(settings_path=service.settings_path)
    try:
        assert restored.snapshot()["tasks"] == []
    finally:
        restored.shutdown()


@pytest.mark.parametrize("race_stage", ["validate-changes", "write-original-backup"])
def test_external_write_during_save_returns_conflict_without_overwrite(
    service, review_result, monkeypatch, race_stage
):
    import pdf2md.review as review

    doc = document(service, review_result)
    external = "外部编辑器在保存期间写入😀\n".encode("utf-8")
    if race_stage == "validate-changes":
        original = review.apply_changes

        def change_after_validation(*args, **kwargs):
            result = original(*args, **kwargs)
            review_result["md"].write_bytes(external)
            return result

        monkeypatch.setattr(review, "apply_changes", change_after_validation)
    else:
        original = review.atomic_write

        def change_after_backup(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if Path(path).name.endswith(".original.md"):
                review_result["md"].write_bytes(external)
            return result

        monkeypatch.setattr(review, "atomic_write", change_after_backup)
    reply = save(
        service,
        review_result,
        doc,
        markdown="新草稿" + doc["markdown"],
        changes=[{"from": 0, "to": 0, "insert": "新草稿"}],
        updates={key: True for key in flags(doc)},
    )
    assert not reply["ok"] and reply["conflict"]
    assert not reply.get("markdown_saved", False)
    assert review_result["md"].read_bytes() == external
    assert review_result["task"].status == "succeeded_with_warnings"


def test_exported_task_report_has_review_summary_and_preserves_original_report(
    service, review_result, tmp_path
):
    original_report = review_result["folder"] / "conversion-report.json"
    before = original_report.read_bytes()
    doc = document(service, review_result)
    assert save(service, review_result, doc, updates={"w1": True})["ok"]
    exported_path = tmp_path / "tasks-report.json"
    assert service.export_report(str(exported_path))["ok"]
    report = json.loads(exported_path.read_text("utf-8"))
    task = next(item for item in report["tasks"] if item["id"] == review_result["task"].id)
    assert task["reviewed_count"] == 1 and task["pending_count"] == 2
    assert task["status"] == "succeeded_with_warnings"
    assert len(task["warnings"]) == 3
    assert original_report.read_bytes() == before
