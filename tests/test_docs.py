"""Keep docs-only checks useful without relying on a Markdown package or network."""

import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "check_docs", Path(__file__).parents[1] / "scripts" / "check_docs.py"
)
assert SPEC and SPEC.loader
docs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(docs)


def test_badge_links_and_external_urls(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").touch()
    readme = tmp_path / "README.md"
    readme.write_text("[![Python](https://example.test/badge.svg)](pyproject.toml)\n")
    assert docs.check_document(readme, tmp_path) == []


def test_missing_image_is_reported(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("![flow](docs/missing.png)\n")
    assert "docs/missing.png" in docs.check_document(readme, tmp_path)[0]


def test_examples_in_code_fences_are_not_links(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("```markdown\n[example](not-a-real-file.md)\n```\n")
    assert docs.check_document(readme, tmp_path) == []


def test_badge_destination_is_checked(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("[![Python](https://example.test/badge.svg)](missing.toml)\n")
    assert "missing.toml" in docs.check_document(readme, tmp_path)[0]
