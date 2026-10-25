from __future__ import annotations

import os
import sys
from pathlib import Path

from platformdirs import user_cache_path, user_config_path


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def resource_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", project_root()))


def model_root() -> Path:
    configured = os.environ.get("PDF2MD_MODEL_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    choices = [
        resource_root() / "models",
        Path(sys.executable).parent / "models",
        Path(sys.executable).parent.parent / "Resources" / "models",
        project_root() / "models",
    ]
    return next((path for path in choices if (path / "manifest.json").is_file()), choices[0])


def web_root() -> Path:
    return Path(__file__).parent / "web"


def config_file() -> Path:
    return user_config_path("PDF2MD", appauthor=False, ensure_exists=True) / "settings.json"


def cache_root() -> Path:
    return user_cache_path("PDF2MD", appauthor=False, ensure_exists=True)
