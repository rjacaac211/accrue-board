from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError

from accrueboard.api import app as app_module


def test_health_reports_database_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "get_engine", lambda: create_engine("sqlite://"))
    response = TestClient(app_module.create_app()).get("/api/health")
    assert response.status_code == 200
    assert response.json()["database"] == "ok"


def test_health_reports_database_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenEngine:
        def connect(self) -> None:
            raise OperationalError("SELECT 1", {}, Exception("refused"))

    monkeypatch.setattr(app_module, "get_engine", BrokenEngine)
    response = TestClient(app_module.create_app()).get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.1.0", "database": "unavailable"}


def test_frontend_routes_fall_back_to_index(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "index.html").write_text("<html>app</html>")
    (tmp_path / "asset.js").write_text("console.log(1)")
    monkeypatch.setattr(app_module.get_settings(), "frontend_dist", str(tmp_path))
    client = TestClient(app_module.create_app())
    assert client.get("/").text == "<html>app</html>"
    assert client.get("/tasks/task_123").text == "<html>app</html>"
    assert client.get("/asset.js").text == "console.log(1)"
    assert client.get("/api/does-not-exist").status_code == 404
