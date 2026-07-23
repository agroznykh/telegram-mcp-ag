#!/usr/bin/env python3
"""Rebuild telegram-digest.zip from SKILL.md -- rerun after editing SKILL.md.

The zip's internal layout (a top-level telegram-digest/ folder containing
SKILL.md) is what claude.ai's "Upload a skill" dialog expects; this is the
one place that layout is decided, so install.sh/install.ps1 and the README's
direct-download link all serve exactly this file, never a hand-made one.
"""
import zipfile
from pathlib import Path

HERE = Path(__file__).parent
SKILL_MD = HERE / "telegram-digest" / "SKILL.md"
OUT = HERE / "telegram-digest.zip"

with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
    zf.write(SKILL_MD, arcname="telegram-digest/SKILL.md")

print(f"Wrote {OUT}")
