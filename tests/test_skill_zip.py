"""telegram-digest-skill-<version>.zip is a checked-in build artifact (not
generated at install time) so install.sh/install.ps1 can serve it without a
zip tool on the user's machine. This guards it against going stale relative
to the SKILL.md it's built from, or carrying another version's name.
"""
import hashlib
import re
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / ".claude" / "skills"
SKILL_MD = SKILLS_DIR / "telegram-digest" / "SKILL.md"

# Claude Desktop shows this as the skill's Version, and the installers read it for
# the zip's name.
METADATA_VERSION = re.compile(r'^metadata:\n  version: "(\d+\.\d+)"\n', re.MULTILINE)

# Each version's text, hashed without the version. A new version gets a new
# entry; a known one must not change, which is what forces the version bump.
SKILL_VERSIONS = {
    "1.0": "76da59981a698e55488a901f63f20330311ca6766c9c1579d6634160deea61d2",
    "1.1": "35ccc94beb465a96c746f758919f493b6dfadbdf4af4c4c8cc37d9f941cda466",
}


def test_skill_text_changes_come_with_a_new_version():
    text = SKILL_MD.read_bytes().replace(b"\r\n", b"\n").decode("utf-8")
    match = METADATA_VERSION.search(text)
    assert match, "SKILL.md must have metadata.version in its frontmatter"

    digest = hashlib.sha256(METADATA_VERSION.sub("", text, count=1).encode("utf-8")).hexdigest()
    assert SKILL_VERSIONS.get(match.group(1)) == digest, (
        "SKILL.md changed: raise metadata.version, add the new version and this hash "
        f"to SKILL_VERSIONS, and rebuild the zip. Hash: {digest}"
    )


def test_only_the_current_versions_zip_is_checked_in():
    version = METADATA_VERSION.search(SKILL_MD.read_text(encoding="utf-8")).group(1)
    assert sorted(p.name for p in SKILLS_DIR.glob("telegram-digest*.zip")) == [
        f"telegram-digest-skill-{version}.zip"
    ], "rerun 'python3 .claude/skills/build_zip.py' after raising metadata.version"


def test_skill_zip_matches_source():
    version = METADATA_VERSION.search(SKILL_MD.read_text(encoding="utf-8")).group(1)
    with zipfile.ZipFile(SKILLS_DIR / f"telegram-digest-skill-{version}.zip") as zf:
        assert zf.namelist() == ["telegram-digest/SKILL.md"]
        zipped = zf.read("telegram-digest/SKILL.md")

    # Compare modulo line endings: build_zip.py always writes LF (matching
    # the git blob raw.githubusercontent.com serves), but a checkout on a
    # CI runner with core.autocrlf on (e.g. windows-latest) rewrites the
    # working-tree SKILL.md to CRLF -- that's a checkout artifact, not a
    # real mismatch between the zip and its source.
    assert zipped.replace(b"\r\n", b"\n") == SKILL_MD.read_bytes().replace(b"\r\n", b"\n"), (
        "the skill zip is stale -- rerun "
        "'python3 .claude/skills/build_zip.py' after editing SKILL.md"
    )
