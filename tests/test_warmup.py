"""Warmup lifecycle and upstream cache contracts without creating ONNX sessions."""

from __future__ import annotations

import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from pdf2md.domain import Settings
from pdf2md.warming import observe_model_loading, prepare_pipeline, warmup_profile


def fake_models(monkeypatch):
    """Match RapidDoc 0.9.11 cache keys and its nested table OCR factory."""
    prefix = "rapid_doc.backend.pipeline."
    init = ModuleType(prefix + "model_init")
    names = ModuleType(prefix + "model_list")
    pipeline = ModuleType(prefix + "pipeline_analyze")
    created, pipeline_calls = [], []
    atoms, aggregates = {}, {}

    def freeze(value):
        return json.dumps(value, sort_keys=True)

    class AtomManager:
        def get_atom_model(self, atom_model_name, **kwargs):
            name = atom_model_name
            if name in ("layout", "formula", "table"):
                config_name = {"layout": "layout_config", "formula": "formula_config",
                               "table": "table_config"}[name]
                key = (name, freeze(kwargs.get(config_name)))
            elif name == "ocr":
                key = (name, freeze(kwargs.get("ocr_config")), kwargs.get("lang"),
                       kwargs.get("det_db_box_thresh", 0.3),
                       kwargs.get("det_db_unclip_ratio", 1.8),
                       kwargs.get("enable_merge_det_boxes", True), kwargs.get("is_seal", True))
            else:
                key = name
            if key not in atoms:
                atoms[key] = init.atom_model_init(model_name=name, **kwargs)
            return atoms[key]

    def factory(model_name, **kwargs):
        created.append((model_name, kwargs.copy()))
        if model_name == "table":
            AtomManager().get_atom_model(
                "ocr", lang=kwargs.get("lang"), ocr_config=kwargs.get("ocr_config"),
                det_db_box_thresh=0.5, det_db_unclip_ratio=1.6, enable_merge_det_boxes=False,
            )
        return SimpleNamespace(orientation_engine=object(), name=model_name)

    class PipelineManager:
        def get_model(self, **kwargs):
            pipeline_calls.append(kwargs.copy())
            key = freeze(kwargs)
            if key not in aggregates:
                atoms = AtomManager()
                atoms.get_atom_model("layout", layout_config=kwargs["layout_config"])
                if kwargs["formula_enable"]:
                    atoms.get_atom_model("formula", formula_config={
                        "enable": kwargs["formula_enable"], **kwargs["formula_config"]})
                atoms.get_atom_model("ocr", lang=kwargs["lang"], ocr_config=kwargs["ocr_config"])
                # RapidDoc defaults to native DocVortex table rules, so the
                # aggregate intentionally skips constructing a table model.
                aggregates[key] = object()
            return aggregates[key]

    init.atom_model_init = factory
    init.AtomModelSingleton = AtomManager
    names.AtomicModel = SimpleNamespace(ImgOrientationCls="img_ori_cls", Table="table")
    pipeline.ModelSingleton = PipelineManager
    for name, module in (("model_init", init), ("model_list", names), ("pipeline_analyze", pipeline)):
        monkeypatch.setitem(sys.modules, prefix + name, module)

    class Engine:
        lang = "ch"
        formula_enable = True
        layout_config = {"markdown_ignore_labels": ["header", "footer"]}
        ocr_config = {"Det.model_path": "local-det.onnx", "Rec.model_path": "local-rec.onnx"}
        formula_config = {"model_dir_or_path": "local-formula.onnx"}
        table_config = {"unet.model_dir_or_path": "local-table.onnx"}

        def warmup(self, lang=None, formula_enable=None, table_enable=None):
            return PipelineManager().get_model(
                lang=lang or self.lang, formula_enable=formula_enable,
                table_enable=table_enable, layout_config=self.layout_config,
                ocr_config=self.ocr_config, formula_config=self.formula_config,
                table_config=self.table_config,
            )

    return SimpleNamespace(init=init, created=created, calls=pipeline_calls,
                           engine=Engine(), factory=factory, pipeline=PipelineManager)


def test_profile_ignores_document_output_and_parse_options():
    assert warmup_profile(Settings()) == {"remove_headers": True, "formula": True}
    assert warmup_profile(Settings(mode="ocr", page_range="3", extract_images=False,
                                   table_format="html", output_dir="elsewhere")) == warmup_profile(Settings())
    assert warmup_profile({"remove_headers": False, "formula": False}) == {
        "remove_headers": False, "formula": False}


def test_prepare_loads_true_sessions_cache_keys_and_first_run_reuses(monkeypatch):
    models = fake_models(monkeypatch)
    phases = []
    orientation = prepare_pipeline(models.engine, Settings(), phases.append)
    names = [name for name, _kwargs in models.created]
    assert set(names) == {"layout", "ocr", "formula", "table", "img_ori_cls"}
    assert len([name for name in names if name == "ocr"]) == 3
    assert any(call["lang"] == "ch" for call in models.calls)
    assert any(call["lang"] is None for call in models.calls)
    assert models.init.atom_model_init is models.factory
    table_calls = [kwargs for name, kwargs in models.created if name == "table"]
    assert len(table_calls) == 1
    assert table_calls[0]["table_config"] == models.engine.table_config
    assert "enable" not in table_calls[0]["table_config"]
    assert "预热表格识别模型" in phases and "预热公式识别模型" in phases
    assert phases[-1] == "预热页面方向模型"
    created = len(models.created)
    models.pipeline().get_model(
        lang=None, formula_enable=True, table_enable=True,
        layout_config=models.engine.layout_config, ocr_config=models.engine.ocr_config,
        formula_config=models.engine.formula_config, table_config=models.engine.table_config,
    )
    assert len(models.created) == created
    # The first scanned table's actual lazy fallback key is now populated.
    models.init.AtomModelSingleton().get_atom_model(
        "table", lang=models.engine.lang, ocr_config=models.engine.ocr_config,
        table_config=models.engine.table_config,
    )
    assert len(models.created) == created
    assert prepare_pipeline(models.engine, Settings(), phases.append) is orientation
    assert len(models.created) == created
    # Cache hits do not generate another set of misleading loading callbacks.
    assert phases.count("预热公式识别模型") == 1


def test_disabled_formula_still_preloads_it_and_actual_disabled_run_cache(monkeypatch):
    models = fake_models(monkeypatch)
    prepare_pipeline(models.engine, Settings(formula=False), lambda _phase: None)
    assert any(name == "formula" for name, _ in models.created)
    assert any(call["lang"] is None and not call["formula_enable"] for call in models.calls)
    previous = len(models.created)
    prepare_pipeline(models.engine, Settings(formula=True), lambda _phase: None)
    assert len(models.created) == previous


def test_observer_restores_upstream_factory_on_failure(monkeypatch):
    models = fake_models(monkeypatch)

    def fail(*_args, **_kwargs):
        raise ValueError("local model failed")

    models.init.atom_model_init = fail
    with pytest.raises(ValueError, match="local model failed"):
        with observe_model_loading(lambda _phase: None):
            models.init.atom_model_init("formula")
    assert models.init.atom_model_init is fail


def test_adapter_warms_once_per_model_profile_and_reuses_orientation(monkeypatch):
    from pdf2md import warming
    from pdf2md.engine import RapidDocAdapter

    adapter = RapidDocAdapter.__new__(RapidDocAdapter)
    adapter._orientation = None
    adapter._warmed_profiles = set()
    adapter.emit = lambda _message: None
    engine = object()
    adapter.configure = lambda _settings: engine
    calls, orientation = [], object()

    def prepare(actual, settings, _phase):
        assert actual is engine
        calls.append(warmup_profile(settings))
        return orientation

    monkeypatch.setattr(warming, "prepare_pipeline", prepare)
    adapter.warmup(Settings())
    adapter.warmup(Settings(mode="ocr", page_range="3"))
    assert len(calls) == 1
    assert adapter._get_orientation() is orientation
    adapter.warmup(Settings(remove_headers=False))
    assert len(calls) == 2


def test_adapter_does_not_mark_failed_profile_warm(monkeypatch):
    from pdf2md import warming
    from pdf2md.engine import RapidDocAdapter

    adapter = RapidDocAdapter.__new__(RapidDocAdapter)
    adapter._orientation, adapter._warmed_profiles = None, set()
    adapter.configure = lambda _settings: object()
    adapter.emit = lambda _message: None

    def fail(*_args):
        raise RuntimeError("session construction failed")

    monkeypatch.setattr(warming, "prepare_pipeline", fail)
    with pytest.raises(RuntimeError):
        adapter.warmup(Settings())
    assert not adapter._warmed_profiles


class Connection:
    def __init__(self, messages):
        self.messages, self.sent, self.closed = list(messages), [], False

    def recv_bytes(self, _limit):
        if not self.messages:
            raise EOFError()
        return json.dumps(self.messages.pop(0)).encode()

    def send_bytes(self, payload):
        self.sent.append(json.loads(payload))

    def close(self):
        self.closed = True


def test_worker_warmup_reuses_adapter_and_correlates_every_message(monkeypatch, tmp_path):
    from pdf2md import engine
    from pdf2md.worker import worker_main

    initialized = []

    class Adapter:
        def __init__(self, root, emit):
            initialized.append(root)
            self.emit = emit

        def warmup(self, settings):
            self.emit({"type": "phase", "phase": "预热公式识别模型"})
            return warmup_profile(settings)

        def convert(self, _source, _pages, _settings, _staging):
            self.emit({"type": "progress", "completed_pages": 1, "selected_pages": 1})
            return {"warnings": []}

    monkeypatch.setattr(engine, "RapidDocAdapter", Adapter)
    monkeypatch.setattr(engine, "restrict_network", lambda: None)
    connection = Connection([
        {"type": "warmup", "request_id": "warm-1", "generation": 4, "settings": {}},
        {"type": "run", "request_id": "run-2", "generation": 4, "task_id": "task-2",
         "settings": {}, "source": "sample.pdf", "pages": [1], "staging": str(tmp_path)},
        {"type": "warmup", "request_id": "warm-3", "generation": 5, "settings": {}},
        {"type": "shutdown"},
    ])
    worker_main(connection, str(tmp_path / "models"))
    assert len(initialized) == 1 and connection.closed
    grouped = {request: [m for m in connection.sent if m["request_id"] == request]
               for request in ("warm-1", "run-2", "warm-3")}
    assert grouped["warm-1"][-1]["type"] == "warmup_completed"
    assert grouped["run-2"][-1]["type"] == "completed"
    assert any(m["type"] == "progress" for m in grouped["run-2"])
    assert all(m["task_id"] == "task-2" and m["generation"] == 4 for m in grouped["run-2"])
    assert all(not m["task_id"] and m["generation"] == 5 for m in grouped["warm-3"])


def test_worker_reports_warmup_failure_and_invalid_settings_without_pdf_task(monkeypatch, tmp_path):
    from pdf2md import engine
    from pdf2md.worker import worker_main

    class Adapter:
        def __init__(self, _root, _emit):
            pass

        def warmup(self, _settings):
            raise OSError("missing local formula model")

    monkeypatch.setattr(engine, "RapidDocAdapter", Adapter)
    monkeypatch.setattr(engine, "restrict_network", lambda: None)
    connection = Connection([
        {"type": "warmup", "request_id": "first", "generation": 1, "settings": {}},
        {"type": "warmup", "request_id": "second", "generation": 2,
         "settings": {"unexpected_setting": True}},
    ])
    worker_main(connection, str(tmp_path))
    failures = [m for m in connection.sent if m["type"] == "warmup_failed"]
    assert len(failures) == 2
    assert "missing local formula model" in failures[0]["error"]
    assert failures[1]["request_id"] == "second" and failures[1]["generation"] == 2
    assert not any(m["type"] in ("completed", "failed") for m in connection.sent)
    assert not list(tmp_path.iterdir())
