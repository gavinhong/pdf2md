"""Keep installed upstream license texts in the portable distribution."""

import json
import shutil
from importlib.metadata import distributions
from pathlib import Path

root = Path(__file__).resolve().parents[1]
target = root / "licenses"
target.mkdir(exist_ok=True)
for distribution in distributions():
    name = distribution.metadata["Name"]
    for entry in distribution.files or []:
        if any(part.lower().startswith(("license", "copying", "notice")) for part in entry.parts):
            source = distribution.locate_file(entry)
            if source.is_file() and source.stat().st_size < 2_000_000:
                relative = Path(*[part for part in entry.parts if part not in ("..", ".")])
                destination = target / name / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
node_packages = json.loads((root / "package-lock.json").read_text("utf-8"))["packages"]
for relative, metadata in node_packages.items():
    if not relative.startswith("node_modules/") or metadata.get("dev"):
        continue
    package = relative.removeprefix("node_modules/")
    directory = root / relative
    if not directory.is_dir():
        continue
    for source in directory.iterdir():
        if source.is_file() and source.name.lower().startswith(("license", "copying", "notice")):
            destination = target / package / source.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
print(f"License texts collected in {target}")
