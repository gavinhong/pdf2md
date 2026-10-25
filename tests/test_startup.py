"""Deterministic startup/warmup contracts; never load or spawn real models."""

from __future__ import annotations

import json
import threading
import time

import pytest

import pdf2md.service as service_module
from pdf2md.domain import Settings, Task
from pdf2md.service import ApplicationService

MODEL_STATUS = {"ready": True, "directory": "local-models", "count": 12, "bytes": 123, "error": ""}


def profile(settings: Settings) -> dict:
    return {"remove_headers": settings.remove_headers, "formula": settings.formula}


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "background state did not reach the expected value"


@pytest.fixture
def startup_factory(tmp_path, monkeypatch):
    created = []
    monkeypatch.setattr(service_module, "model_status", lambda *_: dict(MODEL_STATUS))
    monkeypatch.setattr(ApplicationService, "_perform_warmup", lambda self, settings: profile(settings))

    def create(*, load=None, deferred=True):
        if load is None:
            def load(self):
                return Settings(), [], dict(MODEL_STATUS)
        monkeypatch.setattr(ApplicationService, "_load_initial_state", load)
        instance = ApplicationService(
            settings_path=tmp_path / "settings.json", models_path=tmp_path / "models", deferred=deferred
        )
        created.append(instance)
        return instance

    yield create
    for instance in created:
        instance.shutdown()


def test_deferred_constructor_does_not_restore_or_check_models(tmp_path, monkeypatch):
    calls = []

    def forbidden(*_):
        calls.append("called")
        raise AssertionError("deferred constructor performed initialization work")

    monkeypatch.setattr(ApplicationService, "_load_initial_state", forbidden)
    monkeypatch.setattr(service_module, "model_status", forbidden)
    service = ApplicationService(settings_path=tmp_path / "settings.json", deferred=True)
    try:
        snapshot = service.snapshot()
        assert snapshot["startup"]["state"] == "pending"
        assert snapshot["warmup"]["state"] == "pending"
        assert snapshot["tasks"] == [] and calls == []
    finally:
        service.shutdown()
    assert calls == []


def test_default_constructor_restores_synchronously_without_warmup(startup_factory, monkeypatch):
    loaded, warmed = [], []

    def load(_self):
        loaded.append(True)
        return Settings(), [Task(id="previous", source="old.pdf", name="old.pdf")], dict(MODEL_STATUS)

    monkeypatch.setattr(
        ApplicationService, "_perform_warmup", lambda self, settings: warmed.append(True)
    )
    service = startup_factory(load=load, deferred=False)
    assert loaded == [True]
    assert service.snapshot()["startup"]["state"] == "ready"
    assert service.snapshot()["tasks"][0]["id"] == "previous"
    assert warmed == []


def test_initialize_is_idempotent_and_restores_once(startup_factory):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def load(_self):
        calls.append(True)
        entered.set()
        assert release.wait(3)
        return Settings(), [Task(id="previous", source="old.pdf", name="old.pdf")], dict(MODEL_STATUS)

    service = startup_factory(load=load)
    try:
        assert service.initialize()["ok"]
        assert entered.wait(1)
        service.initialize()
        service.initialize()
        assert service.snapshot()["startup"]["state"] == "loading"
        release.set()
        wait_for(lambda: service.snapshot()["startup"]["state"] == "ready")
        wait_for(lambda: service.snapshot()["warmup"]["state"] == "ready")
        service.initialize()
        assert calls == [True]
        assert len(service.snapshot()["tasks"]) == 1
    finally:
        release.set()


def test_snapshot_remains_responsive_during_slow_core_restore(startup_factory):
    entered, release = threading.Event(), threading.Event()

    def load(_self):
        entered.set()
        assert release.wait(3)
        return Settings(), [], dict(MODEL_STATUS)

    service = startup_factory(load=load)
    service.initialize()
    assert entered.wait(1)
    result, finished = [], threading.Event()

    def read_snapshot():
        result.append(service.snapshot())
        finished.set()

    reader = threading.Thread(target=read_snapshot)
    reader.start()
    try:
        assert finished.wait(0.75), "snapshot waited for the slow restore to finish"
        assert result[0]["startup"]["state"] == "loading"
        assert result[0]["startup"]["phase"]
    finally:
        release.set()
        reader.join(3)


@pytest.mark.parametrize(
    "operation",
    ["import", "remove", "retry", "start", "save-review", "open-review", "relink-review"],
)
def test_mutations_before_core_ready_are_rejected_without_file_changes(
    startup_factory, tmp_path, operation, monkeypatch
):
    service = startup_factory()
    index = tmp_path / "tasks.json"
    old_index = b'{"tasks":[{"id":"retained"}]}'
    index.write_bytes(old_index)
    md = tmp_path / "existing.md"
    md.write_text("existing output\n", encoding="utf-8")
    (tmp_path / "content_list.json").write_text('{"warnings": []}', encoding="utf-8")

    def forbidden_inspection(*_args):
        raise AssertionError("file inspection ran before core initialization")

    monkeypatch.setattr(service_module, "inspect_pdf", forbidden_inspection)
    actions = {
        "import": lambda: service.import_files([str(tmp_path / "missing.pdf")]),
        "remove": lambda: service.remove(),
        "retry": lambda: service.retry("not-yet-restored"),
        "start": lambda: service.start({"output_dir": str(tmp_path / "out")}),
        "save-review": lambda: service.save_review("not-yet-restored", {}),
        "open-review": lambda: service.open_review_result_path(str(md)),
        "relink-review": lambda: service.locate_review_source_path(
            "not-yet-restored", str(tmp_path / "missing.pdf")
        ),
    }
    response = actions[operation]()
    assert not response["ok"] and response["error"]
    assert any(word in response["error"] for word in ("初始化", "恢复", "准备"))
    assert index.read_bytes() == old_index
    assert service.snapshot()["tasks"] == []
    assert not (tmp_path / "out").exists()


def test_failed_core_restore_is_visible_and_can_be_retried(startup_factory, monkeypatch):
    def fail(_self):
        raise OSError("模拟任务索引暂时不可读")

    service = startup_factory(load=fail)
    service.initialize()
    wait_for(lambda: service.snapshot()["startup"]["state"] == "failed")
    state = service.snapshot()["startup"]
    assert state["error"] and "暂时不可读" in state["error"]
    assert state["phase"]
    monkeypatch.setattr(
        ApplicationService, "_load_initial_state", lambda self: (Settings(), [], dict(MODEL_STATUS))
    )
    assert service.initialize()["ok"]
    wait_for(lambda: service.snapshot()["startup"]["state"] == "ready")
    assert service.snapshot()["startup"]["error"] == ""


def test_shutdown_without_initialization_keeps_existing_task_index(startup_factory, tmp_path):
    service = startup_factory()
    index = tmp_path / "tasks.json"
    data = json.dumps({"tasks": [{"id": "previous"}]}).encode()
    index.write_bytes(data)
    service.shutdown()
    assert index.read_bytes() == data


def test_shutdown_during_restore_never_commits_empty_or_late_task_index(startup_factory, tmp_path):
    entered, release = threading.Event(), threading.Event()

    def load(_self):
        entered.set()
        assert release.wait(3)
        return Settings(), [Task(id="late", source="late.pdf", name="late.pdf")], dict(MODEL_STATUS)

    service = startup_factory(load=load)
    index = tmp_path / "tasks.json"
    data = b'{"tasks":[{"id":"keep-original-index"}]}'
    index.write_bytes(data)
    service.initialize()
    assert entered.wait(1)
    closer = threading.Thread(target=service.shutdown)
    closer.start()
    try:
        time.sleep(0.05)
        assert index.read_bytes() == data
        release.set()
        closer.join(3)
        assert not closer.is_alive()
        # Wait past the restore completion so a late background write is observable.
        time.sleep(0.05)
        assert index.read_bytes() == data
    finally:
        release.set()
        closer.join(3)


@pytest.mark.parametrize("deferred", [False, True])
def test_invalid_task_index_json_reports_failed_and_shutdown_keeps_original(
    tmp_path, monkeypatch, deferred
):
    index = tmp_path / "tasks.json"
    original = b'{"schema_version":"1.0", "tasks":[ invalid JSON'
    index.write_bytes(original)
    monkeypatch.setattr(service_module, "model_status", lambda *_: dict(MODEL_STATUS))
    monkeypatch.setattr(ApplicationService, "_perform_warmup", lambda self, settings: profile(settings))
    service = ApplicationService(settings_path=tmp_path / "settings.json", deferred=deferred)
    try:
        if deferred:
            assert service.initialize()["ok"]
        wait_for(lambda: service.snapshot()["startup"]["state"] == "failed")
        assert service.snapshot()["startup"]["error"]
        assert service.snapshot()["tasks"] == []
        assert index.read_bytes() == original
    finally:
        service.shutdown()
    assert index.read_bytes() == original


def test_warmup_state_and_profile_are_published_without_blocking_core(startup_factory, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def warm(_self, settings):
        entered.set()
        assert release.wait(3)
        return profile(settings)

    monkeypatch.setattr(ApplicationService, "_perform_warmup", warm)
    service = startup_factory()
    service.initialize()
    try:
        assert entered.wait(1)
        snapshot = service.snapshot()
        assert snapshot["startup"]["state"] == "ready"
        assert snapshot["warmup"]["state"] == "loading"
        assert snapshot["warmup"]["phase"]
        prior_revision = snapshot["revision"]
        release.set()
        wait_for(lambda: service.snapshot()["warmup"]["state"] == "ready")
        completed = service.snapshot()
        assert completed["warmup"]["profile"] == profile(Settings())
        assert completed["warmup"]["error"] == ""
        assert completed["revision"] > prior_revision
    finally:
        release.set()


def test_failed_warmup_retry_clears_error_and_does_not_duplicate_work(startup_factory, monkeypatch):
    calls = []
    entered, release = threading.Event(), threading.Event()

    def warm(_self, settings):
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("模拟识别流水线初始化失败")
        entered.set()
        assert release.wait(3)
        return profile(settings)

    monkeypatch.setattr(ApplicationService, "_perform_warmup", warm)
    service = startup_factory()
    service.initialize()
    wait_for(lambda: service.snapshot()["warmup"]["state"] == "failed")
    assert "初始化失败" in service.snapshot()["warmup"]["error"]
    assert service.snapshot()["startup"]["state"] == "ready"
    try:
        assert service.retry_warmup()["ok"]
        assert entered.wait(1)
        service.retry_warmup()
        service.retry_warmup()
        release.set()
        wait_for(lambda: service.snapshot()["warmup"]["state"] == "ready")
        assert calls == [True, True]
        assert service.snapshot()["warmup"]["error"] == ""
    finally:
        release.set()


def test_conversion_queues_during_warmup_and_cancel_keeps_warmup_alive(
    startup_factory, monkeypatch, pdf_path, tmp_path
):
    entered, release = threading.Event(), threading.Event()
    worker_calls = []

    def warm(_self, settings):
        entered.set()
        assert release.wait(4)
        return profile(settings)

    def unexpected_worker(_self):
        worker_calls.append(True)
        raise AssertionError("conversion started before warmup finished")

    monkeypatch.setattr(ApplicationService, "_perform_warmup", warm)
    monkeypatch.setattr(ApplicationService, "_ensure_worker", unexpected_worker)
    service = startup_factory()
    service.initialize()
    try:
        assert entered.wait(1)
        assert service.import_files([str(pdf_path)])["ok"]
        reply = service.start({"output_dir": str(tmp_path / "output"), "page_range": "1"})
        assert reply["ok"] and reply["queued"] == 1
        wait_for(lambda: service.snapshot()["busy"])
        time.sleep(0.05)
        snapshot = service.snapshot()
        assert snapshot["tasks"][0]["status"] == "queued"
        assert snapshot["warmup"]["state"] == "loading"
        assert worker_calls == []
        assert service.cancel(all_tasks=True)["ok"]
        wait_for(lambda: not service.snapshot()["busy"])
        assert service.snapshot()["tasks"][0]["status"] == "cancelled"
        assert service.snapshot()["warmup"]["state"] == "loading"
        assert worker_calls == []
        release.set()
        wait_for(lambda: service.snapshot()["warmup"]["state"] == "ready")
    finally:
        release.set()
