import time

import pytest

from robok.config import Config, MotorsCfg, SafetyCfg, TofCfg
from robok.core import build_robot
from robok.hal.motors import FakeMotors
from robok.hal.tof import FakeTof, TofReading
from robok.services.drive import Drive
from robok.services.obstacles import Obstacles, direction_cm
from tests.test_drive import Clock, run


def R(name, state, cm=None):
    return TofReading(name, state, cm, 0.0)


# --- función pura ---------------------------------------------------------------------------------

def test_delante_cuenta_el_mas_cercano_de_los_frontales():
    r = {"front": R("front", "ok", 80), "front_left": R("front_left", "ok", 31), "front_right": R("front_right", "libre")}
    assert direction_cm(r, ("front", "front_left", "front_right")) == (31, [])


def test_todo_libre_es_none():
    r = {n: R(n, "libre") for n in ("front", "front_left", "front_right")}
    assert direction_cm(r, ("front", "front_left", "front_right")) == (None, [])


@pytest.mark.parametrize("state", ["error", "iniciando"])
def test_un_sensor_fallido_bloquea_esa_direccion(state):
    r = {"front": R("front", "ok", 90), "front_left": R("front_left", state), "front_right": R("front_right", "libre")}
    assert direction_cm(r, ("front", "front_left", "front_right")) == (0.0, ["front_left"])


def test_lo_que_no_esta_instalado_no_cuenta():
    r = {"front": R("front", "ok", 55)}                          # solo hay un ToF frontal y ninguno trasero
    assert direction_cm(r, ("front", "front_left", "front_right")) == (55, [])
    assert direction_cm(r, ("rear",)) == (None, [])


# --- servicio con Drive real ----------------------------------------------------------------------

def make(names=("front", "front_left", "front_right", "rear"), **safety):
    clock, events, sensor_events = Clock(), [], []
    drive = Drive(FakeMotors(), MotorsCfg(), SafetyCfg(**safety), clock=clock, on_event=events.append)
    tof = FakeTof(names)
    obs = Obstacles(tof, drive, on_event=sensor_events.append)
    return obs, tof, drive, clock, sensor_events


def go(obs, drive, clock, seconds, cmd=(0, 1, 1.0)):
    """Como el robot real: el servicio de obstáculos y el latido de la web se repiten cada 40 ms."""
    out = (0.0, 0.0)
    for _ in range(round(seconds / 0.04)):
        clock.advance(0.04)
        obs.update()
        drive.command(*cmd)
        out = drive.tick()
    return out


def test_despejado_permite_avanzar():
    obs, tof, drive, clock, ev = make()
    assert go(obs, drive, clock, 1.0) == pytest.approx((0.4, 0.4))
    assert ev == []


def test_pared_al_frente_frena_y_tambien_la_ve_el_sensor_lateral():
    obs, tof, drive, clock, _ = make()
    go(obs, drive, clock, 1.0)
    tof.set("front_left", 15)                                    # el central no la ve, el lateral sí
    assert go(obs, drive, clock, 0.2) == (0.0, 0.0)
    assert drive.status()["limited"] == "obstaculo_frente" and drive.status()["front_cm"] == 15
    tof.set("front_left", None)
    assert go(obs, drive, clock, 1.0) == pytest.approx((0.4, 0.4))


def test_pared_atras_bloquea_solo_la_reversa():
    obs, tof, drive, clock, _ = make()
    tof.set("rear", 10)
    assert go(obs, drive, clock, 1.0, cmd=(0, -1, 1.0)) == (0.0, 0.0)
    assert go(obs, drive, clock, 1.0, cmd=(0, 1, 1.0)) == pytest.approx((0.4, 0.4))


def test_sensor_frontal_en_falla_bloquea_el_avance_pero_deja_retroceder_y_girar():
    obs, tof, drive, clock, ev = make()
    go(obs, drive, clock, 1.0)
    tof.fail("front_right")
    assert go(obs, drive, clock, 0.3) == (0.0, 0.0)
    assert ev == ["sensor_fallo"]
    assert go(obs, drive, clock, 1.0, cmd=(0, -1, 1.0)) == pytest.approx((-0.4, -0.4))
    out = go(obs, drive, clock, 1.0, cmd=(1, 0, 1.0))
    assert out[0] > 0 > out[1]


def test_el_evento_de_falla_sale_una_vez_y_avisa_al_recuperarse():
    obs, tof, drive, clock, ev = make()
    tof.fail("rear")
    for _ in range(5):
        obs.update()
    assert ev == ["sensor_fallo"]
    tof.set("rear", None)
    obs.update(); obs.update()
    assert ev == ["sensor_fallo", "sensor_ok"]


def test_sensor_iniciando_bloquea_como_si_hubiera_un_obstaculo():
    obs, tof, drive, clock, _ = make()
    tof.fail("front", state="iniciando")
    assert go(obs, drive, clock, 0.5) == (0.0, 0.0)


def test_solo_un_sensor_frontal_instalado():
    obs, tof, drive, clock, _ = make(names=("front",))
    assert go(obs, drive, clock, 1.0) == pytest.approx((0.4, 0.4))
    tof.set("front", 10)
    assert go(obs, drive, clock, 0.3) == (0.0, 0.0)
    assert go(obs, drive, clock, 1.0, cmd=(0, -1, 1.0)) == pytest.approx((-0.4, -0.4)), "sin ToF trasero no hay límite atrás"


def test_estado_para_la_web():
    obs, tof, drive, clock, _ = make()
    tof.set("front", 42)
    obs.update()
    s = obs.status()
    assert s["front_cm"] == 42 and s["rear_cm"] is None and s["faults"] == []
    assert s["sensors"]["front"] == {"state": "ok", "cm": 42.0, "age_s": 0.0}
    assert (s["stop_cm"], s["slow_cm"], s["rear_stop_cm"]) == (20.0, 35.0, 20.0)


def test_si_el_servicio_muere_el_drive_bloquea_por_falta_de_datos():
    """Fail-safe de extremo a extremo: sin nadie que refresque los ToF, el último 'despejado' caduca."""
    obs, tof, drive, clock, _ = make()
    assert go(obs, drive, clock, 1.0) == pytest.approx((0.4, 0.4))
    assert run(drive, clock, 1.0, refresh=(0, 1, 1.0)) == (0.0, 0.0)   # el servicio dejó de llamar


def test_el_hilo_sobrevive_a_un_fallo_y_sigue_alimentando_al_drive():
    class Flaky(FakeTof):
        calls = 0

        def snapshot(self):
            Flaky.calls += 1
            if Flaky.calls == 2:
                raise RuntimeError("fallo puntual")
            return super().snapshot()

    drive = Drive(FakeMotors(), MotorsCfg(), SafetyCfg())
    obs = Obstacles(Flaky(("front",)), drive, hz=100)
    obs.start()
    try:
        deadline = time.monotonic() + 3
        while Flaky.calls < 6 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert Flaky.calls >= 6 and drive.status()["front_cm"] is None and not drive.status()["obstacles_stale"]
    finally:
        obs.close()


# --- integración con el robot ------------------------------------------------------------------------

def test_con_tof_el_drive_nace_bloqueado_y_se_libera_al_llegar_los_datos():
    robot = build_robot(Config(sim=True, tof=TofCfg(enabled=True)))
    try:
        d = robot.drive
        d.command(0, 1)
        t = time.monotonic()
        assert d.tick(t + 0.4) == (0.0, 0.0) and d.status()["limited"] == "obstaculo_frente", "sin datos: bloqueado"
        robot.start()                                      # arranca los sensores y hace la 1.ª lectura antes de mover
        assert robot.tof.started
        d.command(0, 1)
        out = (0.0, 0.0)
        t = time.monotonic()
        for i in range(1, 12):
            d.command(0, 1)
            out = d.tick(t + i * 0.04)
        assert out[0] > 0 and d.status()["limited"] is None
    finally:
        robot.stop()
    assert robot.tof.closed


def test_sin_tof_configurados_no_hay_servicio_ni_bloqueo():
    robot = build_robot(Config(sim=True))
    try:
        assert robot.tof is None and robot.obstacles is None and robot.status()["tof"] is None
        assert robot.drive.status()["front_cm"] is None
    finally:
        robot.stop()


def test_falla_de_sensor_pone_cara_triste_y_sonido():
    robot = build_robot(Config(sim=True, tof=TofCfg(enabled=True)))
    try:
        robot.on_drive_event("sensor_fallo")
        assert robot.face.state()["expression"] == "sad"
    finally:
        robot.stop()
