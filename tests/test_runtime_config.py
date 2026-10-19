"""Never let a CI credential payload overwrite unrelated settings or inject lines."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/runtime_config.py"
SPEC = importlib.util.spec_from_file_location("runtime_config", SCRIPT)
assert SPEC and SPEC.loader
runtime_config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime_config)


def test_update_preserves_ai_and_replaces_duplicate_qq_values(tmp_path: Path) -> None:
    path = tmp_path / "runtime.env"
    path.write_text("AI_API_KEY=keep\nQQ_ENABLED=false\nQQ_APP_ID=old\nQQ_APP_ID=duplicate\n")
    runtime_config.update(path, {"QQ_APP_ID": "1234", "QQ_APP_SECRET": "literal-$()`#"})
    assert path.read_text() == (
        "AI_API_KEY=keep\nQQ_APP_ID=1234\nQQ_APP_SECRET=literal-$()`#\nQQ_ENABLED=true\n"
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"AI_API_KEY": "overwrite"},
        {"QQ_APP_ID": "1234"},
        {"QQ_APP_ID": "1234", "QQ_APP_SECRET": "secret\nHOST=0.0.0.0"},
        {"QQ_APP_ID": "bad;command", "QQ_APP_SECRET": "secret"},
        {"QQ_APP_ID": "1234", "QQ_APP_SECRET": "secret", "QQ_ENABLED": "true"},
    ],
)
def test_bad_payload_is_rejected_without_credential_output(payload: dict) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "validate"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert not result.stdout
    assert "secret" not in result.stderr


def test_empty_payload_preserves_existing_config(tmp_path: Path) -> None:
    path = tmp_path / "runtime.env"
    path.write_text("QQ_ENABLED=true\n")
    runtime_config.update(path, runtime_config.validate({}))
    assert path.read_text() == "QQ_ENABLED=true\n"
