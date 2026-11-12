"""Controlled Fullish role for the real one-container scenario; synthetic data only."""

import json
import os
import runpy
import sys
import time
from pathlib import Path

from fullish.telemetry import emit

assert os.environ.get("FULLISH_UNIFIED_CI") == "1" and os.getuid() == 10001
assert 10001 in os.getgroups()
control = Path("/var/lib/fullish/control")
(control / "boundary-sentinel").write_text("synthetic-fullish-private-data")
(control / "policy.json").write_text(
    json.dumps(
        {
            "version": 1,
            "defaults": {"chat": True, "image": True, "search": False, "sandbox": False},
            "groups": {},
        }
    )
)
emit("deployment.report.summary_failed", error_type="TimeoutError", phase="model")
gate = Path(os.environ["TMPDIR"]) / "continue-fixture"
deadline = time.monotonic() + 150
while not gate.exists():
    if time.monotonic() >= deadline:
        raise RuntimeError("fixture_gate_timeout")
    time.sleep(0.1)
sys.argv = ["/workspace/scripts/probe_dim_plugin.py", "--endpoint", os.environ["DIM_ENDPOINT"]]
sys.path.insert(0, "/workspace/scripts")
runpy.run_path(sys.argv[0], run_name="__main__")
