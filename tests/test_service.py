import time

from pdf2md.domain import Task


def test_import_duplicates_remove_and_per_file_errors(service, pdf_path, tmp_path):
    reply = service.import_files([str(pdf_path), str(pdf_path), str(tmp_path / "missing.pdf")])
    assert (reply["added"], reply["duplicates"], len(reply["errors"])) == (1, 1, 1)
    assert service.snapshot()["tasks"][0]["pages"] == 3
    service.remove()
    assert not service.snapshot()["tasks"]
    assert pdf_path.exists()


def test_missing_models_are_actionable(service, pdf_path, tmp_path):
    service.models_path = tmp_path / "absent-models"
    service.import_files([str(pdf_path)])
    response = service.start({"output_dir": str(tmp_path / "out")})
    assert not response["ok"] and "模型" in response["error"]
    assert not service.busy


def test_invalid_range_can_be_corrected_and_retried(service, pdf_path, tmp_path):
    service.import_files([str(pdf_path)])
    reply = service.start({"output_dir": str(tmp_path / "out"), "page_range": "4"})
    assert reply["queued"] == 0
    task = service.snapshot()["tasks"][0]
    assert task["status"] == "failed" and "范围" in task["error"]
    assert service.retry(task["id"])["ok"]
    assert service.snapshot()["tasks"][0]["status"] == "draft"


def test_worker_failure_does_not_abandon_following_tasks(service, pdf_path, tmp_path, monkeypatch):
    service.import_files([str(pdf_path)])
    service.tasks.append(Task(id="second", source=str(pdf_path), name="second.pdf", pages=3))

    def fail():
        raise RuntimeError("test worker startup failure")

    monkeypatch.setattr(service, "_ensure_worker", fail)
    assert service.start({"output_dir": str(tmp_path / "out"), "page_range": "1"})["ok"]
    service.batch_thread.join(10)
    assert not service.busy
    assert [task.status for task in service.tasks] == ["failed", "failed"]
    assert not list((tmp_path / "out" / ".pdf2md-tmp").glob("*/document.md"))


def test_cancel_real_spawned_worker_leaves_no_partial_result(service, pdf_path, tmp_path):
    service.import_files([str(pdf_path)])
    assert service.start({"output_dir": str(tmp_path / "out"), "page_range": "1"})["ok"]
    deadline = time.monotonic() + 5
    while not service.current and time.monotonic() < deadline:
        time.sleep(0.02)
    service.cancel(all_tasks=True)
    service.batch_thread.join(10)
    assert not service.busy
    assert service.tasks[0].status == "cancelled"
    assert service.process is None
    assert not list((tmp_path / "out").glob("*/*.md"))
