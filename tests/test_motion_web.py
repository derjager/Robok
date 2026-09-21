import json
import time

import pytest

import robok.core as core
from robok.config import Config, MotorsCfg
from robok.hal.motors import FakeMotors, Tb6612Motors
from tests.conftest import TOKEN
from tests.test_motors import FakeGpio


def settle(robot, seconds=0.44, dt=0.04):
    """Hace correr el bucle de control a mano (el hilo no está arrancado en las pruebas).

    0,44 s alcanzan para la rampa completa (2,5/s) y quedan por debajo del watchdog (0,5 s): con 0,5 s el
    último paso ya dispararía el watchdog y pararía los motores."""
    t = time.monotonic()
    for i in range(1, round(seconds / dt) + 1):
        robot.drive.tick(t + i * dt)


def reply(ws):
    return next(m for m in iter(ws.receive_json, None) if m["type"] != "state")


@pytest.mark.parametrize("path", ["/api/drive", "/api/stop", "/api/estop", "/api/estop/reset"])
def test_manejo_exige_token(client, path):
    assert client.post(path, json={"x": 0, "y": 1}).status_code == 401
    assert client.post(path, json={"x": 0, "y": 1}, headers={"X-Wally-Token": "otro"}).status_code == 401


def test_drive_por_rest_mueve_los_motores_simulados(client, auth, robot):
    r = client.post("/api/drive", json={"x": 0, "y": 1}, headers=auth)
    assert r.status_code == 200 and r.json()["ok"] is True
    settle(robot)
    m = robot.drive.motors
    assert (m.left, m.right) == pytest.approx((0.4, 0.4))
    client.post("/api/stop", headers=auth)
    assert (m.left, m.right) == (0, 0)


@pytest.mark.parametrize("body", [{"x": 2, "y": 0}, {"x": 0, "y": -1.5}, {"x": 0, "y": 0, "scale": 3},
                                  {"x": "a", "y": 0}, {"y": 1}])
def test_drive_rechaza_valores_invalidos(client, auth, body):
    assert client.post("/api/drive", json=body, headers=auth).status_code == 422


def test_estop_por_rest_bloquea_y_reanuda(client, auth, robot):
    client.post("/api/drive", json={"x": 0, "y": 1}, headers=auth)
    settle(robot)
    assert client.post("/api/estop", headers=auth).json()["estop"] is True
    assert (robot.drive.motors.left, robot.drive.motors.right) == (0, 0)
    assert client.post("/api/drive", json={"x": 0, "y": 1}, headers=auth).json()["ok"] is False
    assert client.post("/api/estop/reset", headers=auth).json()["estop"] is False
    assert client.get("/api/state", headers=auth).json()["drive"]["estop"] is False


def test_estado_incluye_motores_y_drive(client, auth):
    s = client.get("/api/state", headers=auth).json()
    assert s["motors"].startswith("simulados") and s["drive"]["max_duty"] == 0.4
    assert {"left", "right", "estop", "stale", "limited"} <= set(s["drive"])


def test_el_paro_hace_reaccionar_a_la_cara(client, auth, robot):
    client.post("/api/estop", headers=auth)
    assert robot.face.state()["expression"] == "angry"


# --- WebSocket ------------------------------------------------------------------------------
def test_ws_drive_estop_y_reanudar(client, robot):
    with client.websocket_connect(f"/ws?token={TOKEN}") as ws:
        ws.send_text(json.dumps({"type": "drive", "x": 0, "y": 1, "scale": 1}))
        assert reply(ws) == {"type": "ack", "ok": True}
        ws.send_text(json.dumps({"type": "estop"}))
        assert reply(ws)["ok"] is True
        ws.send_text(json.dumps({"type": "drive", "x": 0, "y": 1}))
        assert reply(ws) == {"type": "ack", "ok": False}, "en paro las órdenes se rechazan"
        ws.send_text(json.dumps({"type": "estop_reset"}))
        assert reply(ws)["ok"] is True
        ws.send_text(json.dumps({"type": "drive", "x": 0, "y": 1}))
        assert reply(ws)["ok"] is True


def test_ws_el_estado_incluye_el_drive(client):
    with client.websocket_connect(f"/ws?token={TOKEN}") as ws:
        assert ws.receive_json()["drive"]["estop"] is False


def test_ws_valor_no_finito_se_rechaza_y_para(client, robot):
    with client.websocket_connect(f"/ws?token={TOKEN}") as ws:
        ws.send_text(json.dumps({"type": "drive", "x": 0, "y": 1}))
        reply(ws)
        settle(robot)
        assert robot.drive.motors.left > 0
        ws.send_text('{"type": "drive", "x": NaN, "y": 0}')       # json.loads de Python lo acepta
        assert reply(ws)["type"] == "error"
        assert (robot.drive.motors.left, robot.drive.motors.right) == (0, 0)


def test_ws_desconectar_para_el_robot(client, robot):
    with client.websocket_connect(f"/ws?token={TOKEN}") as ws:
        ws.send_text(json.dumps({"type": "drive", "x": 0, "y": 1}))
        reply(ws)
        settle(robot)
        assert robot.drive.motors.left > 0
    assert (robot.drive.motors.left, robot.drive.motors.right) == (0, 0), "cliente fuera = motores parados"


# --- construcción segura de los motores -------------------------------------------------------
def test_por_defecto_los_motores_son_simulados():
    motors, desc = core.build_motors(Config())
    assert isinstance(motors, FakeMotors) and "enabled = false" in desc


def test_sim_gana_aunque_motors_este_habilitado():
    motors, desc = core.build_motors(Config(sim=True, motors=MotorsCfg(enabled=True)))
    assert isinstance(motors, FakeMotors) and "--sim" in desc


def test_habilitado_construye_el_tb6612(monkeypatch):
    gpio = FakeGpio()
    monkeypatch.setattr(core, "LgpioBackend", lambda chip: gpio)
    motors, desc = core.build_motors(Config(motors=MotorsCfg(enabled=True, max_duty=0.3)))
    assert isinstance(motors, Tb6612Motors) and "TB6612" in desc and "30%" in desc
    assert gpio.levels and all(v == 0 for v in gpio.levels.values())


def test_si_lgpio_falla_cae_a_simulado_y_lo_dice(monkeypatch):
    def boom(chip):
        raise RuntimeError("no hay /dev/gpiochip0")
    monkeypatch.setattr(core, "LgpioBackend", boom)
    motors, desc = core.build_motors(Config(motors=MotorsCfg(enabled=True)))
    assert isinstance(motors, FakeMotors) and desc.startswith("ERROR") and "gpiochip0" in desc


def test_si_el_driver_falla_a_medias_se_libera_el_gpio(monkeypatch):
    gpio = FakeGpio()

    def explode(pin, level=0):
        raise RuntimeError("pin ocupado")
    gpio.claim_output = explode
    monkeypatch.setattr(core, "LgpioBackend", lambda chip: gpio)
    motors, desc = core.build_motors(Config(motors=MotorsCfg(enabled=True)))
    assert isinstance(motors, FakeMotors) and gpio.closed, "no debe quedar el chip GPIO abierto"


def test_apagar_el_robot_detiene_y_cierra_los_motores(robot):
    robot.start()
    robot.drive.command(0, 1)
    time.sleep(0.2)
    robot.stop()
    assert robot.drive.motors.closed and robot.drive.motors.left == 0
