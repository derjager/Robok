import io
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from robok.config import Config, VisionCfg
from robok.core import build_robot
from robok.web.app import create_app
from tests.conftest import TOKEN
from tests.vision_helpers import CAT, CAT_RIGHT, frame

H = {"X-Wally-Token": TOKEN}


@pytest.fixture
def vrobot():
    r = build_robot(Config(sim=True, vision=VisionCfg(teach_samples=3)))
    yield r
    r.stop()


@pytest.fixture
def vc(vrobot):
    return TestClient(create_app(vrobot, TOKEN))


def feed(robot, jpeg, n=1, dt=0.8):
    t = getattr(robot, "_t", 1000.0)
    for _ in range(n):
        t += dt
        robot.vision.process_frame(jpeg, t)
    robot._t = t


ENDPOINTS = [("get", "/api/vision"), ("post", "/api/vision/settings"), ("get", "/api/identities"),
             ("post", "/api/identities"), ("patch", "/api/identities/g-x"), ("delete", "/api/identities/g-x"),
             ("post", "/api/identities/g-x/teach"), ("post", "/api/vision/teach/cancel"),
             ("delete", "/api/identities/g-x/samples/abcdef012345"), ("get", "/api/identities/g-x/samples/abcdef012345.jpg")]


@pytest.mark.parametrize("method,path", ENDPOINTS)
def test_todo_exige_token(vc, method, path):
    assert getattr(vc, method)(path).status_code == 401
    assert getattr(vc, method)(path, headers={"X-Wally-Token": "otro"}).status_code == 401


def test_estado_incluye_la_vision(vc, vrobot):
    s = vc.get("/api/state", headers=H).json()["vision"]
    assert s["available"] and s["enabled"] and s["identities"] == 0 and s["teach"] is None
    assert vc.get("/api/vision", headers=H).json() == vrobot.vision.status()


def test_crear_listar_renombrar_seguir_y_borrar(vc):
    r = vc.post("/api/identities", json={"name": "Pila", "kind": "cat"}, headers=H)
    assert r.status_code == 200
    ident = r.json()["identity"]
    assert (ident["id"], ident["name"], ident["kind_es"], ident["follow"], ident["samples"]) == ("g-pila", "Pila", "gato", True, [])
    assert r.json()["teach"] is None
    assert vc.get("/api/identities", headers=H).json()["identities"][0]["name"] == "Pila"
    r = vc.patch("/api/identities/g-pila", json={"follow": False, "name": "Pilar"}, headers=H).json()
    assert (r["name"], r["follow"]) == ("Pilar", False)
    assert vc.delete("/api/identities/g-pila", headers=H).json() == {"ok": True}
    assert vc.get("/api/identities", headers=H).json()["identities"] == []
    assert vc.delete("/api/identities/g-pila", headers=H).status_code == 422


@pytest.mark.parametrize("body", [{"name": "Rex", "kind": "dog"}, {"name": "", "kind": "cat"}, {"name": "x" * 65, "kind": "cat"},
                                  {"kind": "cat"}, {"name": "Ok", "kind": "cat", "follow": "quizás"}, {"name": "a\x00b", "kind": "cat"}])
def test_crear_rechaza_datos_invalidos(vc, body):
    assert vc.post("/api/identities", json=body, headers=H).status_code == 422


def test_nombre_repetido_da_422_con_el_motivo(vc):
    vc.post("/api/identities", json={"name": "Pila", "kind": "cat"}, headers=H)
    r = vc.post("/api/identities", json={"name": "pila", "kind": "cat"}, headers=H)
    assert r.status_code == 422 and "ya existe" in r.json()["detail"]


@pytest.mark.parametrize("path", ["/api/identities/..%2F..%2Fetc/teach", "/api/identities/G_MAYUS/teach", "/api/identities/x y/teach"])
def test_ids_raros_dan_404_sin_tocar_el_disco(vc, path):
    assert vc.post(path, json={}, headers=H).status_code in (404, 422)


def test_ensenar_de_punta_a_punta_por_la_api(vc, vrobot):
    r = vc.post("/api/identities", json={"name": "Pila", "kind": "cat", "follow": True, "teach": True}, headers=H).json()
    assert r["teach"]["state"] == "active" and r["teach"]["target"] == 3
    for p in (0.0, 0.3, 0.6):
        feed(vrobot, frame(cats=[CAT], patch=p))
    st = vc.get("/api/vision", headers=H).json()
    assert st["teach"]["state"] == "done" and st["teach"]["collected"] == 3
    ident = vc.get("/api/identities", headers=H).json()["identities"][0]
    assert len(ident["samples"]) == 3
    # miniaturas: por cabecera y por ?token= (una etiqueta <img> no manda cabeceras)
    sid = ident["samples"][0]
    r = vc.get(f"/api/identities/g-pila/samples/{sid}.jpg?token={TOKEN}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    Image.open(io.BytesIO(r.content)).verify()
    # y borrar una muestra
    after = vc.delete(f"/api/identities/g-pila/samples/{sid}", headers=H).json()
    assert len(after["samples"]) == 2 and vc.get(f"/api/identities/g-pila/samples/{sid}.jpg", headers=H).status_code == 404


def test_ensenar_mas_a_una_identidad_existente(vc, vrobot):
    vc.post("/api/identities", json={"name": "Pila", "kind": "cat"}, headers=H)
    r = vc.post("/api/identities/g-pila/teach", json={"samples": 5}, headers=H)
    assert r.status_code == 200 and r.json()["teach"]["target"] == 5
    assert vc.post("/api/identities/g-pila/teach", json={}, headers=H).status_code == 409, "ya hay una en curso"
    assert vc.post("/api/vision/teach/cancel", headers=H).json()["teach"]["state"] == "cancelled"
    assert vc.post("/api/identities/g-pila/teach", json={"samples": 2}, headers=H).status_code == 422
    assert vc.post("/api/identities/g-nadie/teach", json={}, headers=H).status_code == 422


def test_borrar_la_identidad_cancela_su_ensenanza(vc, vrobot):
    vc.post("/api/identities", json={"name": "Pila", "kind": "cat", "teach": True}, headers=H)
    vc.delete("/api/identities/g-pila", headers=H)
    assert vrobot.vision.status()["teach"]["state"] == "cancelled"
    feed(vrobot, frame(cats=[CAT]), n=2)
    assert vrobot.vision.gallery.list() == []


def test_apagar_la_vision_bloquea_ensenar_y_se_puede_volver_a_encender(vc, vrobot):
    assert vc.post("/api/vision/settings", json={"enabled": False}, headers=H).json()["enabled"] is False
    r = vc.post("/api/identities", json={"name": "Pila", "kind": "cat", "teach": True}, headers=H).json()
    assert r["teach"] is None and "apagada" in r["teach_error"], "la identidad se crea igual"
    assert vc.post("/api/identities/g-pila/teach", json={}, headers=H).status_code == 409
    assert vc.post("/api/vision/settings", json={"enabled": True, "gaze_mirror": False}, headers=H).json()["gaze_mirror"] is False
    assert vc.post("/api/identities/g-pila/teach", json={}, headers=H).status_code == 200


def test_sin_modelos_no_se_puede_encender_ni_ensenar(vc, vrobot):
    vrobot.vision.unavailable = "faltan los modelos: ejecuta scripts/get_models.py"
    r = vc.post("/api/vision/settings", json={"enabled": True}, headers=H)
    assert r.status_code == 409 and "get_models" in r.json()["detail"]
    assert vc.get("/api/identities", headers=H).status_code == 200, "las identidades se pueden ver igual"


def test_vision_desactivada_da_404():
    r = build_robot(Config(sim=True, vision=VisionCfg(enabled=False)))
    try:
        c = TestClient(create_app(r, TOKEN))
        for m, p in (("get", "/api/vision"), ("get", "/api/identities")):
            assert getattr(c, m)(p, headers=H).status_code == 404
        assert c.get("/api/state", headers=H).json()["vision"] is None
    finally:
        r.stop()


def test_websocket_empuja_las_detecciones(vc, vrobot):
    with vc.websocket_connect(f"/ws?token={TOKEN}") as ws:
        vrobot.vision.process_frame(frame(cats=[CAT_RIGHT]), 1000.0)
        msgs = []
        for _ in range(6):
            m = ws.receive_json()
            msgs.append(m)
            if m["type"] == "vision":
                break
        v = next(m for m in msgs if m["type"] == "vision")
        assert v["tracks"][0]["kind"] == "cat" and v["tracks"][0]["box"][0] == pytest.approx(0.7, abs=0.01)
        assert vrobot.vision._subs, "hay un suscriptor mientras el cliente está conectado"
    deadline = time.time() + 3
    while vrobot.vision._subs and time.time() < deadline:
        time.sleep(0.02)
    assert vrobot.vision._subs == [], "al desconectarse se da de baja"
