"""Lightweight warmup profiles and version-bound RapidDoc cache preparation."""

from __future__ import annotations

import functools
import importlib
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .domain import Settings

_loading_lock = threading.RLock()


def warmup_profile(settings: Settings | dict) -> dict[str, bool]:
    """Only these options change this adapter's model configuration/cache key."""
    if isinstance(settings, dict):
        return {"remove_headers": bool(settings.get("remove_headers", True)),
                "formula": bool(settings.get("formula", True))}
    return {"remove_headers": settings.remove_headers, "formula": settings.formula}


@contextmanager
def observe_model_loading(phase):
    """Observe real cache misses; restore the factory even on a loading error.

    No inference runs concurrently with warmup in the worker. Wrapping the
    underlying factory instead of get_atom_model avoids reporting cached models
    as if they were being loaded again.
    """
    module = importlib.import_module("rapid_doc.backend.pipeline.model_init")
    labels = {"layout": "预热版面识别模型", "formula": "预热公式识别模型",
              "ocr": "预热文字识别模型", "table": "预热表格识别模型",
              "img_ori_cls": "预热页面方向模型"}
    with _loading_lock:
        original = module.atom_model_init
        stack = []

        @functools.wraps(original)
        def observed(model_name, **kwargs):
            label = labels.get(model_name, "预热本地识别模型")
            stack.append(label)
            phase(label)
            try:
                return original(model_name=model_name, **kwargs)
            finally:
                stack.pop()
                if stack:
                    phase(stack[-1])

        module.atom_model_init = observed
        try:
            yield
        finally:
            module.atom_model_init = original


def prepare_pipeline(engine, settings: Settings, phase):
    """Load native sessions using the exact caches used by real conversion.

    RapidDoc's public warmup uses lang='ch', while BatchAnalyze requests the
    pipeline aggregate with lang=None. Warm both keys. The second aggregate
    reuses the atom-level model instances and only fills any missing OCR key.
    Formula is preloaded even when currently disabled, so enabling it later
    doesn't require loading its large session for the first time.
    """
    from rapid_doc.backend.pipeline.model_init import AtomModelSingleton
    from rapid_doc.backend.pipeline.model_list import AtomicModel
    from rapid_doc.backend.pipeline.pipeline_analyze import ModelSingleton

    with observe_model_loading(phase):
        engine.warmup(lang=engine.lang, formula_enable=True, table_enable=True)
        manager = ModelSingleton()
        flags = [True] if settings.formula else [True, False]
        for formula in flags:
            manager.get_model(
                lang=None, formula_enable=formula, table_enable=True,
                layout_config=engine.layout_config, ocr_config=engine.ocr_config,
                formula_config=engine.formula_config, table_config=engine.table_config,
            )
        atoms = AtomModelSingleton()
        # Native/rule table parsing is enabled by default upstream. In that
        # profile public warmup deliberately leaves table_model=None, while the
        # first scanned table calls _process_single_table's lazy fallback using
        # the *raw* table_config (without the aggregate's added enable flag).
        # Preload that exact cache key rather than merely warming an aggregate.
        atoms.get_atom_model(
            atom_model_name=AtomicModel.Table, lang=engine.lang,
            ocr_config=engine.ocr_config, table_config=engine.table_config,
        )
        return atoms.get_atom_model(
            atom_model_name=AtomicModel.ImgOrientationCls
        ).orientation_engine
