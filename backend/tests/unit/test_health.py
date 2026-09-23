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
