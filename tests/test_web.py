import json
import os
import stat

import pytest
from starlette.websockets import WebSocketDisconnect

from robok.config import Config, WebCfg
from robok.web.auth import resolve_token
from tests.conftest import TOKEN


def test_health_es_publico(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["ok"] is True


@pytest.mark.parametrize("method,path", [
    ("get", "/api/state"), ("get", "/api/expressions"), ("get", "/api/face.png"),
    ("post", "/api/face"), ("post", "/api/say"), ("post", "/api/sound"), ("post", "/api/voice/stop"),
])
def test_todo_lo_que_actua_exige_token(client, method, path):
    assert getattr(client, method)(path).status_code == 401
    assert getattr(client, method)(path, headers={"X-Wally-Token": "otro"}).status_code == 401


def test_estado_con_token(client, auth):
    r = client.get("/api/state", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Wally" and body["sim"] is True and body["face"]["expression"] == "neutral"


def test_token_por_query_para_la_imagen(client):
    r = client.get(f"/api/face.png?token={TOKEN}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_cambiar_expresion(client, auth, robot):
    r = client.post("/api/face", json={"expression": "happy"}, headers=auth)
    assert r.status_code == 200 and r.json()["expression"] == "happy"
    assert robot.face.state()["expression"] == "happy"


def test_expresion_invalida_es_422(client, auth):
    r = client.post("/api/face", json={"expression": "furioso"}, headers=auth)
    assert r.status_code == 422 and "desconocida" in r.json()["detail"]


def test_hold_fuera_de_rango_es_422(client, auth):
    assert client.post("/api/face", json={"expression": "sad", "hold": 9999}, headers=auth).status_code == 422


def test_hablar_y_sonidos(client, auth):
    assert client.post("/api/say", json={"text": "hola"}, headers=auth).json() == {"queued": True}
    assert client.post("/api/say", json={"text": ""}, headers=auth).status_code == 422
    assert client.post("/api/sound", json={"name": "ok"}, headers=auth).json() == {"queued": True}
    assert client.post("/api/sound", json={"name": "zzz"}, headers=auth).status_code == 422


def test_la_interfaz_se_sirve_sin_secretos(client):
    r = client.get("/")
    assert r.status_code == 200 and "Wally" in r.text and TOKEN not in r.text


# --- WebSocket ------------------------------------------------------------------------------
def test_websocket_rechaza_token_malo(client):
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("/ws?token=malo"):
            pass
    assert e.value.code == 4401


def test_websocket_cara_mirada_y_estado(client, robot):
    with client.websocket_connect(f"/ws?token={TOKEN}") as ws:
        first = ws.receive_json()
        assert first["type"] == "state" and first["name"] == "Wally"
        ws.send_text(json.dumps({"type": "face", "expression": "surprised", "id": 7}))
        ack = next(m for m in iter(ws.receive_json, None) if m["type"] != "state")
        assert ack == {"type": "ack", "ok": True, "id": 7}
        ws.send_text(json.dumps({"type": "look", "x": 0.5, "y": -0.5}))
        assert next(m for m in iter(ws.receive_json, None) if m["type"] != "state")["ok"] is True
    assert robot.face.state()["expression"] == "surprised"


def test_websocket_errores_no_cierran_la_conexion(client):
    with client.websocket_connect(f"/ws?token={TOKEN}") as ws:
        for payload in ["esto no es json", "[1, 2]", json.dumps({"type": "volar"}),
                        json.dumps({"type": "face"}), json.dumps({"type": "face", "expression": "furioso"}),
                        json.dumps({"type": "look", "x": "abc", "y": 0})]:
            ws.send_text(payload)
            reply = next(m for m in iter(ws.receive_json, None) if m["type"] != "state")
            assert reply["type"] == "error", payload
        ws.send_text(json.dumps({"type": "ping"}))
        assert next(m for m in iter(ws.receive_json, None) if m["type"] != "state") == {"type": "pong"}


# --- token ----------------------------------------------------------------------------------
def test_token_prioridad_env_config_archivo(monkeypatch, tmp_path):
    f = tmp_path / ".tok"
    cfg = Config(web=WebCfg(token="del-config"))
    monkeypatch.setenv("ROBOK_TOKEN", "del-env")
    assert resolve_token(cfg, f) == "del-env"
    monkeypatch.delenv("ROBOK_TOKEN")
    assert resolve_token(cfg, f) == "del-config"
    assert not f.exists()


def test_token_se_genera_una_vez_y_con_permisos_privados(monkeypatch, tmp_path):
    monkeypatch.delenv("ROBOK_TOKEN", raising=False)
    f = tmp_path / ".tok"
    t1 = resolve_token(Config(), f)
    assert len(t1) >= 8 and resolve_token(Config(), f) == t1
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
