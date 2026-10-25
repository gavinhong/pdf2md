"""Exercise real service coordination with an in-memory, process-free worker."""

from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import pdf2md.service as service_module
from pdf2md.domain import Settings
from pdf2md.service import ApplicationService
from pdf2md.warming import warmup_profile

MODELS = {"ready": True, "directory": "mock-models", "count": 12, "bytes": 12, "error": ""}


def wait_for(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "fake worker coordination did not reach the expected state"


class FakeProcess:
    def __init__(self, number):
        self.pid = 900_000 + number
        self.alive = True
        self.terminated = False

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.terminated = True
        self.alive = False

    def kill(self):
        self.terminate()

    def join(self, timeout=None):
        pass


class FakeConnection:
    def __init__(self, host, process):
        self.host, self.process = host, process
        self.incoming = queue.Queue()
        self.buffer = None
        self.closed = False
        self.owner = None
        self.request = None
        self.received = []

    def send_bytes(self, data):
        assert not self.closed
        assert self.owner is None, "multiple concurrent worker requests"
        self.owner = threading.get_ident()
        self.request = json.loads(data)
        with self.host.lock:
            self.host.active += 1
            self.host.max_active = max(self.host.max_active, self.host.active)
            self.host.requests.append((self, self.request))
        if self.request["type"] == "warmup" and not self.host.hold_warmup:
            self.reply(self.request, "warmup_completed", profile=warmup_profile(self.request["settings"]))
        elif self.request["type"] == "run" and not self.host.hold_run:
            self.host.complete_run()

    def poll(self, timeout=0):
        assert self.owner == threading.get_ident(), "a second thread received the same worker pipe"
        if self.closed:
            raise EOFError()
        if self.buffer is not None:
            return True
        try:
            self.buffer = self.incoming.get(timeout=timeout)
        except queue.Empty:
            return False
        if self.buffer is None:
            raise EOFError()
        return True

    def recv_bytes(self, maxlength=None):
        assert self.owner == threading.get_ident(), "worker response read by another IO owner"
        assert self.buffer is not None
        message, self.buffer = self.buffer, None
        self.received.append(message)
        matching = all(message.get(key) == self.request.get(key)
                       for key in ("request_id", "generation", "task_id"))
        if matching and message["type"] in ("completed", "failed", "warmup_completed", "warmup_failed"):
            self.owner = None
            with self.host.lock:
                self.host.active -= 1
        return json.dumps(message).encode("utf-8")

    def reply(self, request, kind, **values):
        self.incoming.put({"type": kind, "request_id": request.get("request_id"),
                           "generation": request.get("generation"), "task_id": request.get("task_id"),
                           **values})

    def close(self):
        if not self.closed:
            self.closed = True
            if self.owner is not None:
                self.owner = None
                with self.host.lock:
                    self.host.active -= 1
            self.incoming.put(None)


class FakeWorker:
    def __init__(self, service, *, hold_warmup=True, hold_run=False):
        self.service = service
        self.hold_warmup, self.hold_run = hold_warmup, hold_run
        self.lock = threading.Lock()
        self.requests, self.processes, self.connections = [], [], []
        self.active, self.max_active = 0, 0

    def ensure(self):
        with self.service.worker_lock:
            if self.service.closed:
                raise InterruptedError("program closed")
            if self.service.process is not None and self.service.process.is_alive():
                return
            process = FakeProcess(len(self.processes))
            connection = FakeConnection(self, process)
            self.processes.append(process)
            self.connections.append(connection)
            self.service.process, self.service.connection = process, connection
            self.service.worker_generation += 1

    def sent(self, kind):
        with self.lock:
            return [(connection, message) for connection, message in self.requests if message["type"] == kind]

    def complete_warmup(self, index=0):
        connection, request = self.sent("warmup")[index]
        connection.reply(request, "warmup_completed", profile=warmup_profile(request["settings"]))

    def complete_run(self, index=-1):
        connection, request = self.sent("run")[index]
        staging = Path(request["staging"])
        (staging / "document.md").write_bytes("# 模拟转换结果😀\n".encode("utf-8"))
        (staging / "content_list.json").write_text(
            json.dumps({"schema_version": "1.1", "source": {}, "pages": [], "blocks": [], "warnings": []}),
            encoding="utf-8",
        )
        connection.reply(request, "completed", report={"warnings": [], "settings": request["settings"]})


@pytest.fixture
def fake_service(tmp_path, monkeypatch):
    created = []
    monkeypatch.setattr(service_module, "model_status", lambda *_: dict(MODELS))
    monkeypatch.setattr(
        ApplicationService, "_load_initial_state", lambda self: (Settings(), [], dict(MODELS))
    )
    # No OS process exists: the real teardown still calls process.terminate and closes the Pipe.
    monkeypatch.setattr(service_module.psutil, "Process", lambda *_: SimpleNamespace(children=lambda **_: []))

    def create(**kwargs):
        service = ApplicationService(settings_path=tmp_path / "settings.json", deferred=True)
        worker = FakeWorker(service, **kwargs)
        monkeypatch.setattr(service, "_ensure_worker", worker.ensure)
        created.append(service)
        assert service.initialize()["ok"]
        wait_for(lambda: bool(worker.sent("warmup")))
        return service, worker

    yield create
    for service in created:
        service.shutdown()


def queue_pdf(service, pdf_path, tmp_path, **settings):
    assert service.import_files([str(pdf_path)])["ok"]
    reply = service.start({"output_dir": str(tmp_path / "converted"), "page_range": "1", **settings})
    assert reply["ok"] and reply["queued"] == 1
    return service.tasks[0]


def test_queued_conversion_automatically_runs_after_warmup_with_same_worker(
    fake_service, pdf_path, tmp_path
):
    service, worker = fake_service()
    task = queue_pdf(service, pdf_path, tmp_path)
    assert task.status == "queued" and worker.sent("run") == []
    worker.complete_warmup()
    wait_for(lambda: not service.snapshot()["busy"])
    assert task.status == "succeeded", task.error
    assert Path(task.markdown_file).read_text("utf-8") == "# 模拟转换结果😀\n"
    assert len(worker.processes) == 1
    assert [request["type"] for _, request in worker.requests] == ["warmup", "run"]
    assert worker.max_active == 1


def test_different_settings_profile_prepares_again_before_running(fake_service, pdf_path, tmp_path):
    service, worker = fake_service(hold_warmup=False)
    wait_for(lambda: service.snapshot()["warmup"]["state"] == "ready")
    worker.hold_warmup = True
    task = queue_pdf(service, pdf_path, tmp_path, formula=False, remove_headers=False)
    wait_for(lambda: len(worker.sent("warmup")) == 2)
    assert task.status == "queued" and worker.sent("run") == []
    second = worker.sent("warmup")[1][1]
    assert not second["settings"]["formula"] and not second["settings"]["remove_headers"]
    worker.complete_warmup(1)
    wait_for(lambda: not service.snapshot()["busy"])
    assert task.status == "succeeded", task.error
    assert service.snapshot()["warmup"]["profile"] == {"formula": False, "remove_headers": False}
    assert worker.max_active == 1


def test_failed_warmup_keeps_queue_and_explicit_retry_resumes_same_task(
    fake_service, pdf_path, tmp_path
):
    service, worker = fake_service()
    task = queue_pdf(service, pdf_path, tmp_path)
    connection, request = worker.sent("warmup")[0]
    connection.reply(request, "warmup_failed", error="模拟模型会话准备失败")
    wait_for(lambda: service.snapshot()["warmup"]["state"] == "failed")
    assert service.snapshot()["busy"] and task.status == "queued"
    assert worker.sent("run") == [] and worker.processes[0].terminated
    assert service.retry_warmup()["ok"]
    wait_for(lambda: len(worker.sent("warmup")) == 2)
    assert task.status == "queued" and len(service.tasks) == 1
    worker.complete_warmup(1)
    wait_for(lambda: not service.snapshot()["busy"])
    assert task.status == "succeeded", task.error
    assert len(worker.sent("run")) == 1


def test_stale_messages_do_not_complete_requests_or_steal_pipe_owner(fake_service, pdf_path, tmp_path):
    service, worker = fake_service(hold_run=True)
    task = queue_pdf(service, pdf_path, tmp_path)
    connection, warmup = worker.sent("warmup")[0]
    connection.reply({**warmup, "request_id": "old-request"}, "warmup_completed", profile={})
    connection.reply({**warmup, "generation": warmup["generation"] - 1}, "phase", phase="过期预热阶段")
    connection.reply({**warmup, "request_id": "old-request"}, "warmup_failed", error="过期失败")
    wait_for(lambda: len(connection.received) == 3)
    assert service.snapshot()["warmup"]["state"] == "loading"
    assert service.snapshot()["warmup"]["phase"] != "过期预热阶段"
    assert worker.sent("run") == []
    worker.complete_warmup()
    wait_for(lambda: len(worker.sent("run")) == 1)
    _, run = worker.sent("run")[0]
    connection.reply({**run, "task_id": "wrong-task"}, "progress", completed_pages=99)
    connection.reply({**run, "generation": run["generation"] - 1}, "completed", report={})
    connection.reply({**run, "request_id": "old-request"}, "failed", error="过期转换失败")
    wait_for(lambda: len(connection.received) == 7)
    assert task.status == "running" and task.completed_pages == 0
    connection.reply(run, "phase", phase="模拟识别正文")
    connection.reply(run, "progress", completed_pages=1)
    wait_for(lambda: task.phase == "模拟识别正文" and task.completed_pages == 1)
    worker.complete_run()
    wait_for(lambda: not service.snapshot()["busy"])
    assert task.status == "succeeded", task.error
    assert worker.max_active == 1


def test_cancel_running_task_stops_worker_and_invalidates_old_readiness(
    fake_service, pdf_path, tmp_path
):
    service, worker = fake_service(hold_warmup=False, hold_run=True)
    wait_for(lambda: service.snapshot()["warmup"]["state"] == "ready")
    task = queue_pdf(service, pdf_path, tmp_path)
    wait_for(lambda: len(worker.sent("run")) == 1)
    old_generation = worker.sent("run")[0][1]["generation"]
    worker.hold_warmup = True
    assert service.cancel()["ok"]
    wait_for(lambda: not service.snapshot()["busy"] and len(worker.sent("warmup")) == 2)
    assert task.status == "cancelled"
    assert worker.processes[0].terminated and worker.connections[0].closed
    assert service.snapshot()["warmup"]["state"] == "loading"
    assert worker.sent("warmup")[1][1]["generation"] > old_generation
    worker.complete_warmup(1)
    wait_for(lambda: service.snapshot()["warmup"]["state"] == "ready")


def test_shutdown_interrupts_warmup_pipe_and_cannot_restart_worker(fake_service):
    service, worker = fake_service()
    thread = service.warmup_thread
    service.shutdown()
    assert service.closed and not thread.is_alive()
    assert worker.processes[0].terminated and worker.connections[0].closed
    assert not service.retry_warmup()["ok"] and not service.initialize()["ok"]
    time.sleep(0.03)
    assert len(worker.processes) == 1


def test_cancel_queued_task_preserves_live_warmup_pipe(fake_service, pdf_path, tmp_path):
    service, worker = fake_service()
    task = queue_pdf(service, pdf_path, tmp_path)
    process, connection = worker.processes[0], worker.connections[0]
    assert service.cancel(all_tasks=True)["ok"]
    wait_for(lambda: not service.snapshot()["busy"])
    assert task.status == "cancelled"
    assert process.is_alive() and not process.terminated and not connection.closed
    assert service.snapshot()["warmup"]["state"] == "loading"
    assert worker.sent("run") == [] and len(worker.sent("warmup")) == 1
    worker.complete_warmup()
    wait_for(lambda: service.snapshot()["warmup"]["state"] == "ready")


def test_repaired_model_files_are_checked_in_background_on_warmup_retry(tmp_path, monkeypatch):
    health = {**MODELS, "ready": False, "error": "本地模型暂时缺失"}
    checking, release = threading.Event(), threading.Event()

    def check_models(_root):
        if health["ready"]:
            checking.set()
            assert release.wait(3)
        return dict(health)

    monkeypatch.setattr(service_module, "model_status", check_models)
    monkeypatch.setattr(
        ApplicationService, "_load_initial_state", lambda self: (Settings(), [], dict(health))
    )
    monkeypatch.setattr(service_module.psutil, "Process", lambda *_: SimpleNamespace(children=lambda **_: []))
    service = ApplicationService(settings_path=tmp_path / "settings.json", deferred=True)
    worker = FakeWorker(service, hold_warmup=False)
    monkeypatch.setattr(service, "_ensure_worker", worker.ensure)
    retried, responses = threading.Event(), []

    def retry():
        responses.append(service.retry_warmup())
        retried.set()

    retry_thread = threading.Thread(target=retry)
    try:
        assert service.initialize()["ok"]
        wait_for(lambda: service.snapshot()["warmup"]["state"] == "failed")
        assert not worker.requests
        health.update(ready=True, error="")
        retry_thread.start()
        assert checking.wait(1), "retry reused stale missing-model status"
        assert retried.wait(0.75), "retry API synchronously waited for model filesystem checks"
        assert responses[0]["ok"]
        assert service.snapshot()["warmup"]["state"] == "loading"
        release.set()
        wait_for(lambda: service.snapshot()["warmup"]["state"] == "ready")
        assert service.snapshot()["models"]["ready"]
        assert len(worker.sent("warmup")) == 1
    finally:
        release.set()
        if retry_thread.ident is not None:
            retry_thread.join(3)
        service.shutdown()


def test_simultaneous_retry_requests_claim_only_one_warmup(fake_service, monkeypatch):
    service, worker = fake_service()
    connection, request = worker.sent("warmup")[0]
    connection.reply(request, "warmup_failed", error="需要用户重试")
    wait_for(lambda: service.snapshot()["warmup"]["state"] == "failed")
    original_stop = service._stop_worker
    original_warmup = service._perform_warmup
    first_stopping, release_first, second_returned = threading.Event(), threading.Event(), threading.Event()
    warmup_calls, stop_calls, responses = [], [], []

    def concurrent_stop():
        name = threading.current_thread().name
        if name.startswith("simultaneous-retry"):
            stop_calls.append(name)
        if name == "simultaneous-retry-first":
            first_stopping.set()
            assert release_first.wait(3)
        original_stop()

    def counted_warmup(settings):
        warmup_calls.append(True)
        return original_warmup(settings)

    monkeypatch.setattr(service, "_stop_worker", concurrent_stop)
    monkeypatch.setattr(service, "_perform_warmup", counted_warmup)

    def retry_second():
        responses.append(service.retry_warmup())
        second_returned.set()

    callers = [threading.Thread(target=lambda: responses.append(service.retry_warmup()),
                                name="simultaneous-retry-first"),
               threading.Thread(target=retry_second, name="simultaneous-retry-second")]
    try:
        callers[0].start()
        assert first_stopping.wait(1)
        callers[1].start()
        assert second_returned.wait(0.75), "second retry waited behind teardown instead of reusing the claim"
        assert stop_calls == ["simultaneous-retry-first"]
        assert warmup_calls == []
        assert service.snapshot()["warmup"]["state"] == "loading"
        release_first.set()
        for caller in callers:
            caller.join(2)
        assert all(not caller.is_alive() for caller in callers)
        assert len(responses) == 2 and all(reply["ok"] for reply in responses)
        wait_for(lambda: bool(warmup_calls))
        assert warmup_calls == [True], "concurrent retry created more than one warmup owner"
        wait_for(lambda: len(worker.sent("warmup")) == 2)
        worker.complete_warmup(1)
        wait_for(lambda: service.snapshot()["warmup"]["state"] == "ready")
    finally:
        release_first.set()
        for caller in callers:
            if caller.ident is not None:
                caller.join(3)


def test_idle_worker_exit_requires_fresh_warmup_before_next_conversion(fake_service, pdf_path, tmp_path):
    service, worker = fake_service(hold_warmup=False)
    wait_for(lambda: service.snapshot()["warmup"]["state"] == "ready")
    old_generation = worker.sent("warmup")[0][1]["generation"]
    worker.hold_warmup = True
    worker.processes[0].alive = False
    task = queue_pdf(service, pdf_path, tmp_path)
    wait_for(lambda: len(worker.sent("warmup")) == 2)
    assert task.status == "queued" and worker.sent("run") == []
    assert worker.sent("warmup")[1][1]["generation"] > old_generation
    worker.complete_warmup(1)
    wait_for(lambda: not service.snapshot()["busy"])
    assert task.status == "succeeded", task.error


def test_cancel_current_before_batch_claims_task_still_cancels_first_queued(
    fake_service, pdf_path, tmp_path, monkeypatch
):
    service, worker = fake_service()
    entered, release = threading.Event(), threading.Event()
    original_batch = service._batch

    def delayed_batch(*args):
        entered.set()
        assert release.wait(3)
        original_batch(*args)

    monkeypatch.setattr(service, "_batch", delayed_batch)
    try:
        task = queue_pdf(service, pdf_path, tmp_path)
        assert entered.wait(1)
        assert service.current == "" and task.status == "queued"
        assert service.cancel()["ok"]
        release.set()
        wait_for(lambda: not service.snapshot()["busy"])
        assert task.status == "cancelled"
        assert worker.sent("run") == []
        assert worker.processes[0].is_alive() and not worker.connections[0].closed
        worker.complete_warmup()
        wait_for(lambda: service.snapshot()["warmup"]["state"] == "ready")
    finally:
        release.set()
