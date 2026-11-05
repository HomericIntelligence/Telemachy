import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ROADMAP = REPO_ROOT / "docs" / "ROADMAP.md"
ADR_DIR = REPO_ROOT / "docs" / "adr"

REQUIRED_SECTIONS = (
    "Current Release",
    "Next Release",
    "Future",
    "Known Limitations",
)

# Matches any markdown link whose target starts with `adr/` and ends with `.md`,
# regardless of whether the link sits in a paragraph or a table cell.
ADR_LINK_RE = re.compile(r"\[[^\]]+\]\(adr/([^)\s]+\.md)\)")


def _read() -> str:
    assert ROADMAP.is_file(), "docs/ROADMAP.md is missing (issue #167)"
    return ROADMAP.read_text(encoding="utf-8")


def test_roadmap_exists() -> None:
    _read()


def test_roadmap_has_required_sections() -> None:
    text = _read()
    for heading in REQUIRED_SECTIONS:
        assert re.search(rf"^##\s+{re.escape(heading)}\b", text, re.MULTILINE), (
            f"docs/ROADMAP.md missing required `## {heading}` section"
        )


def test_roadmap_references_the_nats_work_item() -> None:
    text = _read().lower()
    assert "nats subscriber" in text
    assert "#92" in text  # parent epic


def test_roadmap_uses_canonical_remote_url() -> None:
    # Guard against the fork-URL defect seen in the prior plan iteration.
    text = _read()
    bad = re.findall(r"https://github\.com/(?!HomericIntelligence/Telemachy)[^/\s)]+/Telemachy", text)
    assert not bad, f"docs/ROADMAP.md links to non-canonical repo URLs: {bad}"


def test_roadmap_adr_links_resolve() -> None:
    text = _read()
    targets = ADR_LINK_RE.findall(text)
    assert targets, "docs/ROADMAP.md has no ADR links — at least ADR-001/002/003 must be referenced"
    for rel in targets:
        assert (ADR_DIR / rel).is_file(), f"docs/ROADMAP.md links to missing ADR: {rel}"


def test_adr_003_present_and_accepted() -> None:
    adr = ADR_DIR / "003-roadmap-artifact.md"
    assert adr.is_file(), "ADR-003 (roadmap artefact) is missing"
    body = adr.read_text(encoding="utf-8")
    assert "**Status:** Accepted" in body
