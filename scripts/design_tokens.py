"""Generate CSS from the normative DESIGN.md frontmatter without extra dependencies."""

import argparse
import re
from pathlib import Path

root = Path(__file__).resolve().parents[1]
frontmatter = (root / "DESIGN.md").read_text(encoding="utf-8").split("---", 2)[1]
section = ""
font_role = "sans"
values = []
for line in frontmatter.splitlines():
    if re.fullmatch(r"[\w-]+:", line):
        section = line[:-1]
    if section == "typography" and re.fullmatch(r"  [\w-]+:", line):
        font_role = line.strip()[:-1]
    match = re.fullmatch(r'  ([\w-]+): "([^"]+)"', line)
    if match and section in ("colors", "rounded", "spacing"):
        prefix = {"colors": "color", "rounded": "radius", "spacing": "space"}[section]
        values.append(f"  --{prefix}-{match[1].lower()}: {match[2]};")
    match = re.fullmatch(r'    fontFamily: "([^"]+)"', line)
    if match:
        values.append(f"  --font-{font_role}: {match[1]};")
content = (
    "/* Generated from DESIGN.md by scripts/design_tokens.py. */\n:root {\n"
    + "\n".join(values)
    + "\n}\n"
)
target = root / "frontend" / "tokens.css"
parser = argparse.ArgumentParser()
parser.add_argument("--check", action="store_true")
args = parser.parse_args()
if args.check:
    if not target.is_file() or target.read_text(encoding="utf-8") != content:
        raise SystemExit("Design tokens are out of date. Run npm run build.")
    print("Design token mapping is current.")
else:
    target.write_text(content, encoding="utf-8")
