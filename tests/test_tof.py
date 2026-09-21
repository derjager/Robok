import time

import pytest

import robok.hal.tof as tofmod
from robok.config import TOF_NAMES, Pins, TofCfg
from robok.hal.tof import FakeTof, TofArray, create_tof
from tests.fakes_vl53 import FakeI2cBus, FakeVl53Device, FakeXshutGpio

PINS = Pins()
XSHUT = dict(zip(TOF_NAMES, PINS.tof_xshut))


def wait_for(cond, timeout=3.0, step=0.01):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


class Rig:
    """4 sensores simulados en un bus con sus XSHUT, y un TofArray que los usa."""

    def __init__(self, sensors=TOF_NAMES, mm=None, **cfg):
        mm = mm or {"front": 500, "front_left": 700, "front_right": 900, "rear": 300}
        self.devs = {n: FakeVl53Device(mm=mm[n]) for n in TOF_NAMES}
        self.bus = FakeI2cBus(self.devs)
        self.gpio = FakeXshutGpio({XSHUT[n]: self.devs[n] for n in TOF_NAMES})
        cfg.setdefault("poll_hz", 50)
        self.array = TofArray(TofCfg(enabled=True, sensors=tuple(sensors), **cfg), PINS,
                              bus_factory=lambda _b: self.bus, gpio_factory=lambda _c: self.gpio, settle_s=0.0)

    def state(self, name):
        return self.array.snapshot()[name]


@pytest.fixture
def rig():
    r = Rig()
    yield r
    r.array.close()


def test_arranque_asigna_una_direccion_a_cada_sensor(rig):
    rig.array.start()
    assert [rig.devs[n].addr for n in TOF_NAMES] == [0x30, 0x31, 0x32, 0x33]
    assert wait_for(lambda: all(r.state == "ok" for r in rig.array.snapshot().values()))
    got = {n: r.cm for n, r in rig.array.snapshot().items()}
    assert got == {"front": 50.0, "front_left": 70.0, "front_right": 90.0, "rear": 30.0}


def test_los_cuatro_xshut_se_apagan_antes_de_encender_ninguno(rig):
    rig.array.start()
    first_high = next(i for i, c in enumerate(rig.gpio.calls) if c[0] == "write" and c[2] == 1)
    lows = {c[1] for c in rig.gpio.calls[:first_high] if c[2] == 0}
    assert lows == set(PINS.tof_xshut), "si alguno naciera en 0x29 mientras se configura otro, chocarían"


def test_nunca_hay_dos_sensores_en_0x29_a_la_vez(rig):
    """El bus simulado lanza error si dos dispositivos responden a la misma dirección."""
    rig.array.start()          # si hubiera choque, el arranque de algún sensor fallaría
    assert wait_for(lambda: rig.array.problems() == {} and all(r.state == "ok" for r in rig.array.snapshot().values()))


def test_solo_arranca_los_sensores_listados_pero_apaga_los_4_xshut():
    r = Rig(sensors=("front", "rear"))
    try:
        r.array.start()
        assert r.array.names == ("front", "rear")
        assert set(r.array.snapshot()) == {"front", "rear"}
        assert {c[1] for c in r.gpio.calls if c[0] == "claim"} == set(PINS.tof_xshut)
        assert not r.devs["front_left"].powered and r.devs["front"].addr == 0x30 and r.devs["rear"].addr == 0x33
    finally:
        r.array.close()


def test_mediana_de_3_descarta_un_pico():
    """Determinista: se cargan las últimas 3 lecturas a mano, sin hilo ni tiempos."""
    r = Rig()
    s = r.array._sensors["front"]
    s.last_ok = time.monotonic()
    s.window.extend([500.0, 500.0, 60.0])                    # el dato más reciente es un pico suelto
    assert r.state("front").cm == 50.0, "la mediana lo descarta; el último dato a secas daría 6 cm"
    s.window.clear(); s.window.extend([500.0, 60.0, 60.0])   # dos de tres cercanos: ahora sí hay algo
    assert r.state("front").cm == 6.0
    s.window.clear(); s.window.extend([500.0, 500.0, float("inf")])
    assert r.state("front").cm == 50.0, "un solo 'sin eco' no vuelve libre el frente"
    s.window.clear(); s.window.extend([500.0, float("inf"), float("inf")])
    assert r.state("front").state == "libre", "dos sin eco de tres: libre"


def test_lejos_o_sin_eco_es_libre(rig):
    rig.array.start()
    rig.devs["front"].mm = 8190
    assert wait_for(lambda: rig.state("front").state == "libre")
    assert rig.state("front").cm is None
    rig.devs["front"].mm = 1500                   # más allá de max_cm (120)
    assert wait_for(lambda: rig.state("front").state == "libre")
    rig.devs["front"].mm = 800
    assert wait_for(lambda: rig.state("front").cm == 80.0)


def test_iniciando_antes_de_la_primera_lectura():
    r = Rig()
    try:
        assert {x.state for x in r.array.snapshot().values()} == {"iniciando"}
    finally:
        r.array.close()


def test_sensor_que_no_arranca_es_error_y_los_demas_siguen():
    r = Rig()
    r.devs["front_left"].busy = True             # no responde
    try:
        r.array.start()
        assert wait_for(lambda: r.state("front").state == "ok" and r.state("rear").state == "ok")
        assert r.state("front_left").state == "error"
        assert "front_left" in r.array.problems() and set(r.array.problems()) == {"front_left"}
        assert not r.devs["front_left"].powered, "un sensor fallido se deja apagado (XSHUT bajo)"
    finally:
        r.array.close()


def test_sensor_fallido_se_reintenta_y_se_recupera(monkeypatch):
    monkeypatch.setattr(tofmod, "RETRY_S", (0.1, 0.2, 0.3))
    r = Rig()
    r.devs["rear"].busy = True
    try:
        r.array.start()
        assert wait_for(lambda: r.state("rear").state == "error")
        r.devs["rear"].busy = False
        assert wait_for(lambda: r.state("rear").state == "ok", timeout=3)
        assert r.devs["rear"].addr == 0x33
    finally:
        r.array.close()


def test_sensor_que_se_cuelga_en_marcha_se_reinicia_solo_el(monkeypatch):
    monkeypatch.setattr(tofmod, "RECOVER_AFTER_S", 0.3)
    monkeypatch.setattr(tofmod, "RETRY_S", (0.1, 0.2, 0.3))
    r = Rig(stale_s=0.2)
    try:
        r.array.start()
        assert wait_for(lambda: all(x.state == "ok" for x in r.array.snapshot().values()))
        r.devs["front"].busy = True
        assert wait_for(lambda: r.state("front").state == "error", timeout=3), "sin datos: error, que bloquea el avance"
        assert r.state("rear").state == "ok", "los demás no se ven afectados"
        r.devs["front"].busy = False
        assert wait_for(lambda: r.state("front").state == "ok", timeout=5), "se reinicia (XSHUT) y se recupera"
        assert r.devs["front"].addr == 0x30
    finally:
        r.array.close()


def test_dato_viejo_pasa_a_error():
    r = Rig(stale_s=0.15)
    try:
        r.array.start()
        assert wait_for(lambda: r.state("front").state == "ok")
        r.array.close()                            # el hilo se detiene: nadie actualiza más
        r.array._sensors["front"].failed = False
        time.sleep(0.3)
        assert r.state("front").state == "error" and r.state("front").age_s > 0.15
    finally:
        r.array.close()


def test_bus_i2c_inexistente_deja_todo_en_error_sin_lanzar():
    def no_bus(_n):
        raise FileNotFoundError("/dev/i2c-1")
    a = TofArray(TofCfg(enabled=True), PINS, bus_factory=no_bus, gpio_factory=lambda c: FakeXshutGpio({}))
    a.start()
    snap = a.snapshot()
    assert {r.state for r in snap.values()} == {"error"}
    assert all("FileNotFoundError" in why for why in a.problems().values())
    a.close()


def test_gpio_ocupado_deja_todo_en_error_y_libera_el_bus():
    bus = FakeI2cBus({})
    a = TofArray(TofCfg(enabled=True), PINS, bus_factory=lambda _b: bus,
                 gpio_factory=lambda _c: FakeXshutGpio({}, fail_claim=True))
    a.start()
    assert {r.state for r in a.snapshot().values()} == {"error"} and bus.closed
    a.close()


def test_cerrar_apaga_todos_los_xshut_y_libera(rig):
    rig.array.start()
    assert wait_for(lambda: rig.state("front").state == "ok")
    rig.array.close()
    assert all(rig.gpio.levels[p] == 0 for p in PINS.tof_xshut)
    assert rig.gpio.closed and rig.bus.closed
    assert not any(d.powered for d in rig.devs.values())
    rig.array.close()                              # idempotente


def test_start_es_idempotente(rig):
    rig.array.start()
    t = rig.array._thread
    rig.array.start()
    assert rig.array._thread is t


# --- ToF simulado ---------------------------------------------------------------------------------

def test_fake_tof_fija_distancias_y_estados():
    f = FakeTof(("front", "rear"))
    assert {r.state for r in f.snapshot().values()} == {"libre"}
    f.set("front", 42.5)
    f.set("rear", 500)                             # más lejos que max_cm: libre
    s = f.snapshot()
    assert (s["front"].state, s["front"].cm) == ("ok", 42.5) and s["rear"].state == "libre"
    f.fail("front")
    assert f.snapshot()["front"].state == "error" and f.problems() == {"front": "simulado"}
    with pytest.raises(KeyError):
        f.set("front_left", 10)
    with pytest.raises(ValueError):
        f.set("front", -1)


def test_create_tof():
    assert create_tof(TofCfg(enabled=False), PINS, sim=False) is None
    assert isinstance(create_tof(TofCfg(enabled=True), PINS, sim=True), FakeTof)
    assert isinstance(create_tof(TofCfg(enabled=True), PINS, sim=False), TofArray)


def test_lectura_como_dict():
    f = FakeTof(("front",))
    f.set("front", 33.0)
    assert f.snapshot()["front"].as_dict() == {"state": "ok", "cm": 33.0, "age_s": 0.0}
