"""Portable entry points for project verification commands."""

import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
python = root / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
if sys.argv[1] == "unit":
    command = [str(python), "-m", "pytest", "-q", "-m", "not integration"]
else:
    candidates = list(
        (Path.home() / ".codex/plugins/cache").glob(
            "**/frontend-design-premium/scripts/audit_project.py"
        )
    )
    if not candidates:
        raise SystemExit("The optional frontend-design-premium developer skill is not installed.")
    command = [str(python), str(sorted(candidates)[-1]), str(root), "--mode", "strict"]
raise SystemExit(subprocess.call(command, cwd=root))
