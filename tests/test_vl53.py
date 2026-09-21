import hashlib

import pytest

from robok.hal.vl53l0x import DEFAULT_ADDRESS, NO_TARGET_MM, VL53L0X, Vl53Error
from tests.fakes_vl53 import FakeI2cBus, FakeVl53Device


def make(mm=345, spad=0x8C, **kw):
    dev = FakeVl53Device(mm=mm, spad=spad)
    dev.powered = True
    bus = FakeI2cBus({"a": dev})
    return VL53L0X(bus, DEFAULT_ADDRESS, **kw), dev, bus


def test_secuencia_de_arranque_identica_a_la_de_adafruit():
    """Golden: estas 137 escrituras (con la tabla de ajuste de ST) son las mismas, una a una, que hace
    Adafruit_CircuitPython_VL53L0X 3.6.19; se comprobó contra el original sobre este mismo simulador.
    Si este hash cambia, se tocó la inicialización: hay que volver a compararla con el original."""
    d, dev, _ = make()
    d.start_continuous()
    d.set_address(0x31)
    writes = [(op[1], op[2].hex()) for op in dev.log if op[0] == "w"]
    assert len(writes) == 137
    assert hashlib.sha256(repr(writes).encode()).hexdigest() == \
        "4de19f6e5beb9c26c8347817913179cc4dde8f386cc997ebc7c53d2bade0a735"


@pytest.mark.parametrize("spad", [0x8C, 0x0C, 0x05, 0x91])
def test_arranca_con_cualquier_mapa_de_spad(spad):
    d, _, _ = make(spad=spad)
    d.start_continuous()
    assert d.read_mm() == 345


def test_mide_en_continuo_y_lee_sin_bloquear():
    d, dev, _ = make(mm=123)
    d.start_continuous()
    assert d.read_mm() == 123
    dev.mm = 456
    assert d.read_mm() == 456, "cada lectura limpia la interrupción y el siguiente dato llega solo"


def test_sin_dato_listo_devuelve_none():
    d, dev, _ = make()
    dev.hold = 3
    d.start_continuous()
    reads = [d.read_mm() for _ in range(6)]
    assert reads[:3] == [None, None, None] and 345 in reads


def test_sin_nada_a_la_vista_el_sensor_da_8190_o_mas():
    d, dev, _ = make(mm=8190)
    d.start_continuous()
    assert d.read_mm() >= NO_TARGET_MM


def test_cambio_de_direccion():
    d, dev, bus = make()
    d.start_continuous()
    d.set_address(0x31)
    assert dev.addr == 0x31 and d.address == 0x31
    assert d.read_mm() == 345, "sigue funcionando en la dirección nueva"
    with pytest.raises(OSError):
        bus.read(0x29, 0xC0, 1)                 # ya no responde en 0x29
    assert d.ident_ok()


def test_no_responde():
    dev = FakeVl53Device()          # sin alimentar
    with pytest.raises(Vl53Error, match="no responde"):
        VL53L0X(FakeI2cBus({"a": dev}))


def test_dispositivo_que_no_es_un_vl53l0x():
    dev = FakeVl53Device()
    dev.powered = True
    dev.regs[(0, 0xC0)] = 0x55
    with pytest.raises(Vl53Error, match="no es un VL53L0X"):
        VL53L0X(FakeI2cBus({"a": dev}))


def test_error_de_bus_al_leer_se_convierte_en_vl53error():
    d, _, bus = make()
    d.start_continuous()
    bus.fail_all = True
    with pytest.raises(Vl53Error, match="I2C"):
        d.read_mm()


def test_error_de_bus_durante_el_arranque():
    dev = FakeVl53Device()
    dev.powered = True
    bus = FakeI2cBus({"a": dev})
    real = bus.write
    calls = {"n": 0}

    def flaky(addr, reg, data):
        calls["n"] += 1
        if calls["n"] == 20:
            raise OSError(5, "Input/output error")
        return real(addr, reg, data)
    bus.write = flaky
    with pytest.raises(Vl53Error, match="I2C"):
        VL53L0X(bus)


def test_tiempo_agotado_si_el_sensor_nunca_termina():
    dev = FakeVl53Device()
    dev.powered = True
    dev.hold = 10**9                    # jamás da el dato de calibración
    dev.regs[(7, 0x83)] = 0
    orig = dev._rd

    def stuck(reg):
        return 0 if reg in (0x13, 0x83) else orig(reg)
    dev._rd = stuck
    with pytest.raises(Vl53Error, match="tiempo agotado"):
        VL53L0X(FakeI2cBus({"a": dev}), io_timeout_s=0.1)


def test_presupuesto_de_tiempo_razonable():
    d, _, _ = make()
    assert 20_000 <= d.timing_budget_us <= 200_000


def test_detener_la_medicion():
    d, dev, _ = make()
    d.start_continuous()
    d.stop_continuous()
    assert dev.continuous is False
