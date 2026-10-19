"""Exercise documentation routing against real Git histories and file moves."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "classify_changes", Path(__file__).parents[1] / "scripts" / "classify_changes.py"
)
assert SPEC and SPEC.loader
changes = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(changes)


def git(*args: str) -> str:
    return subprocess.check_output(
        [
            "git",
            "-c",
            "user.name=CI Test",
            "-c",
            "user.email=ci@example.test",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        text=True,
        stderr=subprocess.STDOUT,
    ).strip()


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    git("init", "-b", "main")
    (tmp_path / "README.md").write_text("# Example\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "persona.md").write_text("Runtime prompt\n")
    git("add", ".")
    git("commit", "-m", "initial")
    return tmp_path


def commit() -> str:
    git("add", "--all")
    git("commit", "-m", "update")
    return git("rev-parse", "HEAD")


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "CONTRIBUTING.md",
        "docs/assets/flow.png",
        "docs/guide.md",
        ".github/pull_request_template.md",
        ".github/ISSUE_TEMPLATE/bug.yml",
    ],
)
def test_documentation_paths_skip_runtime(path: str) -> None:
    assert not changes.needs_runtime([path])


@pytest.mark.parametrize(
    "path",
    [
        "src/persona.md",
        "personas/default.md",
        "Dockerfile",
        "pyproject.toml",
        "uv.lock",
        "scripts/deploy.sh",
        ".github/workflows/pipeline.yml",
        "tests/test_app.py",
    ],
)
def test_runtime_or_configuration_paths_run_full_ci(path: str) -> None:
    assert changes.needs_runtime(["README.md", path])


def test_documentation_only_push(repository: Path) -> None:
    base = git("rev-parse", "HEAD")
    (repository / "README.md").write_text("# Updated\n")
    head = commit()
    paths = changes.changed_paths("push", {"before": base}, head)
    assert paths == ["README.md"]
    assert not changes.needs_runtime(paths)


def test_push_checks_all_commits(repository: Path) -> None:
    base = git("rev-parse", "HEAD")
    (repository / "src" / "persona.md").write_text("Changed runtime prompt\n")
    commit()
    (repository / "README.md").write_text("# Updated docs\n")
    head = commit()
    assert changes.needs_runtime(changes.changed_paths("push", {"before": base}, head))


def test_moving_runtime_file_into_docs_still_runs_full_ci(repository: Path) -> None:
    base = git("rev-parse", "HEAD")
    (repository / "docs").mkdir()
    git("mv", "src/persona.md", "docs/persona.md")
    head = commit()
    paths = changes.changed_paths("push", {"before": base}, head)
    assert paths is not None and "src/persona.md" in paths and "docs/persona.md" in paths
    assert changes.needs_runtime(paths)


def test_pr_ignores_unrelated_base_branch_changes(repository: Path) -> None:
    original = git("rev-parse", "HEAD")
    git("switch", "-c", "docs", original)
    (repository / "README.md").write_text("# Feature docs\n")
    head = commit()
    git("switch", "main")
    (repository / "src" / "persona.md").write_text("Unrelated upstream change\n")
    base = commit()
    event = {"pull_request": {"base": {"sha": base}, "head": {"sha": head}}}
    assert not changes.needs_runtime(changes.changed_paths("pull_request", event, head))


def test_missing_base_and_initial_push_run_full_ci(repository: Path) -> None:
    head = git("rev-parse", "HEAD")
    for base in ("0" * 40, "1" * 40):
        assert changes.needs_runtime(changes.changed_paths("push", {"before": base}, head))


def test_manual_run_is_always_full() -> None:
    assert changes.needs_runtime(changes.changed_paths("workflow_dispatch", {}, "a" * 40))


def test_newer_docs_do_not_suppress_pending_code_deployment(repository: Path) -> None:
    built = git("rev-parse", "HEAD")
    (repository / "README.md").write_text("# Newer documentation\n")
    head = commit()
    assert changes.deployment_is_current(built, head)
    (repository / "src" / "persona.md").write_text("Newer runtime change\n")
    newest = commit()
    assert not changes.deployment_is_current(built, newest)


def test_unknown_deployment_comparison_fails_instead_of_silently_skipping(repository: Path) -> None:
    with pytest.raises(ValueError, match="Cannot compare"):
        changes.deployment_is_current("1" * 40, git("rev-parse", "HEAD"))
