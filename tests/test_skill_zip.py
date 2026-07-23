"""telegram-digest.zip is a checked-in build artifact (not generated at
install time) so both install.sh/install.ps1 and the README's direct-download
link can serve it byte-identical, without a zip tool on the user's machine.
This guards it against going stale relative to the SKILL.md it's built from.
"""
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / ".claude" / "skills"
SKILL_MD = SKILLS_DIR / "telegram-digest" / "SKILL.md"
ZIP_PATH = SKILLS_DIR / "telegram-digest.zip"


def test_skill_zip_matches_source():
    with zipfile.ZipFile(ZIP_PATH) as zf:
        assert zf.namelist() == ["telegram-digest/SKILL.md"]
        zipped = zf.read("telegram-digest/SKILL.md")

    assert zipped == SKILL_MD.read_bytes(), (
        "telegram-digest.zip is stale -- rerun "
        "'python3 .claude/skills/build_zip.py' after editing SKILL.md"
    )
