import pytest
from fastapi.testclient import TestClient

import robok.core as core
from robok.config import Config
from robok.core import build_robot
from robok.web.app import create_app

TOKEN = "test-token"


@pytest.fixture(autouse=True)
def _datos_en_tmp(tmp_path, monkeypatch):
    """Ninguna prueba toca data/ ni models/ reales: las rutas relativas de la config caen en un tmp."""
    monkeypatch.setattr(core, "resolve_dir", lambda p: tmp_path / p)


@pytest.fixture
def robot():
    r = build_robot(Config(sim=True))
    yield r
    r.stop()


@pytest.fixture
def client(robot):
    return TestClient(create_app(robot, TOKEN))


@pytest.fixture
def auth():
    return {"X-Wally-Token": TOKEN}
