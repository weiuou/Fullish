"""Check the public deployment contract without a QQ account or model API key."""

from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from fullish.app import create_app


def test_health_lifecycle_and_build_revision(monkeypatch: MonkeyPatch) -> None:
    revision = "0123456789abcdef0123456789abcdef01234567"
    monkeypatch.setenv("APP_REVISION", revision)
    app = create_app()

    # A process that has not completed startup must not pass deployment readiness.
    client = TestClient(app)
    assert client.get("/health/ready").status_code == 503

    with TestClient(app) as running:
        live = running.get("/health/live")
        assert live.status_code == 200
        assert live.json() == {"status": "ok", "mode": "foundation", "qq_connected": False}

        ready = running.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json() == {
            "status": "ready",
            "mode": "foundation",
            "qq_connected": False,
        }

        # The response identifies the deployed build, even if the environment changes.
        monkeypatch.setenv("APP_REVISION", "changed-after-startup")
        version = running.get("/version")
        assert version.status_code == 200
        assert version.json() == {
            "service": "fullish",
            "version": "0.1.0",
            "revision": revision,
            "mode": "foundation",
        }

    assert client.get("/health/ready").status_code == 503
    client.close()
