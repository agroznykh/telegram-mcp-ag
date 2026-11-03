#!/usr/bin/env python3
"""Rebuild telegram-digest-skill-<version>.zip from SKILL.md -- rerun after editing SKILL.md.

The zip's internal layout (a top-level telegram-digest/ folder containing
SKILL.md) is what claude.ai's "Upload a skill" dialog expects; this is the
one place that layout is decided, so install.sh/install.ps1 serve exactly
this file, never a hand-made one.

The version (metadata.version in SKILL.md) is in the file name, which is
the name the installers save it under in Downloads: uploads of different
versions stay apart without anyone renaming anything. The previous
version's zip is removed, so the folder only ever holds the current one.
"""
import re
import zipfile
from pathlib import Path

HERE = Path(__file__).parent
SKILL_MD = HERE / "telegram-digest" / "SKILL.md"

match = re.search(r'^metadata:\n  version: "(\d+\.\d+)"\n', SKILL_MD.read_text(encoding="utf-8"), re.MULTILINE)
if not match:
    raise SystemExit(f"{SKILL_MD} has no metadata.version")
OUT = HERE / f"telegram-digest-skill-{match.group(1)}.zip"

for old in HERE.glob("telegram-digest*.zip"):
    if old != OUT:
        old.unlink()

with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
    zf.write(SKILL_MD, arcname="telegram-digest/SKILL.md")

print(f"Wrote {OUT}")
