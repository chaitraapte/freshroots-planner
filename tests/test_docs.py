from pathlib import Path

from planner.limitations import LIMITATIONS

ROOT = Path(__file__).resolve().parents[1]


def test_readme_limitations_match_app_list():
    readme = (ROOT / "README.md").read_text()
    for item in LIMITATIONS:
        assert item in readme


def test_no_credentials_in_tracked_files():
    import re
    pat = re.compile(r"postgres(?:ql)?://[^\s:/]+:[^\s@*]{6,}@")
    for p in list(ROOT.rglob("*.py")) + list(ROOT.rglob("*.md")) + [ROOT / ".env.example"]:
        if ".venv" in p.parts:
            continue
        assert not [m for m in pat.findall(p.read_text()) if "PASSWORD" not in m], p
