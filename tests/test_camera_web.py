import io
import re
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from robok.config import CameraCfg, Config, TofCfg
from robok.core import build_robot
from robok.web.app import create_app
from tests.conftest import TOKEN


def wait_for(cond, timeout=3.0, step=0.01):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


def parts(body: bytes) -> list[bytes]:
    """Separa un cuerpo multipart/x-mixed-replace en los JPEG que trae, comprobando sus cabeceras."""
    out = []
    for m in re.finditer(rb"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: (\d+)\r\n\r\n", body):
        n = int(m.group(1))
        chunk = body[m.end():m.end() + n]
        assert len(chunk) == n and body[m.end() + n:m.end() + n + 2] == b"\r\n"
        out.append(chunk)
    return out


@pytest.fixture
def cam_robot():
    r = build_robot(Config(sim=True, camera=CameraCfg(width=160, height=120, fps=30, idle_s=0.1),
                           tof=TofCfg(enabled=True)))
    yield r
    r.stop()


@pytest.fixture
def cam_client(cam_robot):
    return TestClient(create_app(cam_robot, TOKEN))


@pytest.mark.parametrize("path", ["/api/camera.jpg", "/api/camera.mjpg?frames=1"])
def test_camara_exige_token(cam_client, path):
    assert cam_client.get(path).status_code == 401
    assert cam_client.get(path, headers={"X-Wally-Token": "otro"}).status_code == 401


def test_sim_tof_exige_token(cam_client):
    assert cam_client.post("/api/sim/tof", json={"name": "front", "cm": 30}).status_code == 401


def test_foto(cam_client, auth, cam_robot):
    r = cam_client.get("/api/camera.jpg", headers=auth)
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert r.headers["cache-control"] == "no-store"
    assert Image.open(io.BytesIO(r.content)).size == (160, 120)
    assert wait_for(lambda: cam_robot.camera.status()["viewers"] == 0)


def test_token_por_parametro_para_la_etiqueta_img(cam_client):
    r = cam_client.get(f"/api/camera.jpg?token={TOKEN}")
    assert r.status_code == 200


def test_flujo_mjpeg_entrega_fotogramas_validos_y_libera_la_camara(cam_client, auth, cam_robot):
    r = cam_client.get("/api/camera.mjpg?frames=4", headers=auth)
    assert r.status_code == 200
    assert r.headers["content-type"] == "multipart/x-mixed-replace; boundary=frame"
    jpegs = parts(r.content)
    assert len(jpegs) == 4
    for j in jpegs:
        assert Image.open(io.BytesIO(j)).size == (160, 120)
    assert cam_robot.camera.status()["viewers"] == 0, "al terminar el flujo no queda ningún espectador"
    assert wait_for(lambda: not cam_robot.camera.status()["on"], timeout=2), "y la cámara se apaga sola"


def test_frames_fuera_de_rango(cam_client, auth):
    assert cam_client.get("/api/camera.mjpg?frames=0", headers=auth).status_code == 422
    assert cam_client.get("/api/camera.mjpg?frames=100000", headers=auth).status_code == 422


def test_camara_desactivada_da_404():
    r = build_robot(Config(sim=True, camera=CameraCfg(enabled=False)))
    try:
        c = TestClient(create_app(r, TOKEN))
        h = {"X-Wally-Token": TOKEN}
        assert c.get("/api/camera.jpg", headers=h).status_code == 404
        assert c.get("/api/camera.mjpg", headers=h).status_code == 404
        assert c.get("/api/state", headers=h).json()["camera"] is None
    finally:
        r.stop()


def test_camara_que_no_entrega_da_503_con_el_motivo(cam_robot, auth, monkeypatch):
    import robok.web.app as appmod

    async def never(cam, timeout):
        cam._error = "el proceso terminó: camera busy"      # aquí, para que un fotograma simulado no lo borre
        raise appmod.asyncio.TimeoutError()
    monkeypatch.setattr(appmod, "next_frame", never)
    r = TestClient(create_app(cam_robot, TOKEN)).get("/api/camera.jpg", headers=auth)
    assert r.status_code == 503 and "camera busy" in r.json()["detail"]


def test_estado_incluye_camara_y_tof(cam_client, auth):
    s = cam_client.get("/api/state", headers=auth).json()
    assert s["camera"]["desc"].startswith("simulada") and s["camera"]["viewers"] == 0
    assert set(s["tof"]["sensors"]) == {"front", "front_left", "front_right", "rear"}
    assert s["tof"]["stop_cm"] == 20.0


def test_sim_tof_fija_distancias_y_llegan_al_drive(cam_client, auth, cam_robot):
    r = cam_client.post("/api/sim/tof", json={"name": "front_left", "cm": 18.5}, headers=auth)
    assert r.status_code == 200 and r.json()["front_cm"] == 18.5
    assert cam_robot.drive.status()["front_cm"] == 18.5
    assert cam_client.post("/api/sim/tof", json={"name": "front_left", "cm": None}, headers=auth).json()["front_cm"] is None
    r = cam_client.post("/api/sim/tof", json={"name": "rear", "fail": True}, headers=auth).json()
    assert r["faults"] == ["rear"] and r["rear_cm"] == 0.0


@pytest.mark.parametrize("body", [{"name": "nope", "cm": 10}, {"name": "front", "cm": -5}, {"name": "front", "cm": "x"},
                                  {"cm": 10}])
def test_sim_tof_rechaza_datos_invalidos(cam_client, auth, body):
    assert cam_client.post("/api/sim/tof", json=body, headers=auth).status_code == 422


def test_sim_tof_no_existe_fuera_de_la_simulacion_o_sin_tof(auth):
    r = build_robot(Config(sim=True))                  # sin ToF configurados
    try:
        c = TestClient(create_app(r, TOKEN))
        assert c.post("/api/sim/tof", json={"name": "front", "cm": 10}, headers=auth).status_code == 404
    finally:
        r.stop()
