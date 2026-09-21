import time

import pytest

from robok.config import MotorsCfg, SafetyCfg
from robok.hal.motors import FakeMotors
from robok.services.drive import Drive


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def make(**cfg):
    clock, motors, events = Clock(), FakeMotors(), []
    cfg.setdefault("max_duty", 0.4)
    d = Drive(motors, MotorsCfg(**cfg), SafetyCfg(), clock=clock, on_event=events.append)
    return d, motors, clock, events


def run(d, clock, seconds, dt=0.04, refresh=None, obstacles=None):
    """Avanza el reloj; `refresh` (x, y, scale) reenvía la orden cada paso, como hace el latido de la web,
    y `obstacles` (frente, atrás) repite las distancias de los sensores, como hace el servicio de obstáculos."""
    out = (0.0, 0.0)
    for _ in range(round(seconds / dt)):
        clock.advance(dt)
        if refresh:
            d.command(*refresh)
        if obstacles:
            d.set_obstacles(*obstacles)
        out = d.tick()
    return out


def test_adelante_llega_al_tope_y_no_lo_supera():
    d, m, clock, _ = make()
    d.command(0, 1)
    out = run(d, clock, 1.0, refresh=(0, 1, 1.0))
    assert out == pytest.approx((0.4, 0.4)) and (m.left, m.right) == pytest.approx((0.4, 0.4))
    assert max(abs(a) for a, _ in m.history) <= 0.4 + 1e-9


def test_rampa_de_aceleracion():
    d, _, clock, _ = make(ramp_per_s=2.5)
    d.command(0, 1)
    clock.advance(0.04)
    assert d.tick()[0] == pytest.approx(0.4 * 0.1)              # 2.5/s * 0.04 s = 0.1 de la escala
    clock.advance(0.04)
    assert d.tick()[0] == pytest.approx(0.4 * 0.2)


def test_soltar_el_joystick_desacelera_por_rampa():
    d, _, clock, _ = make()
    run(d, clock, 1.0, refresh=(0, 1, 1.0))
    d.command(0, 0)
    clock.advance(0.04)
    out = d.tick()
    assert 0 < out[0] < 0.4, "soltar no debe cortar en seco, pero sí bajar"
    assert run(d, clock, 1.0, refresh=(0, 0, 1.0)) == (0.0, 0.0)


def test_zona_muerta_y_escala_de_velocidad():
    d, _, clock, _ = make()
    d.command(0.03, 0.05)
    assert run(d, clock, 0.5, refresh=(0.03, 0.05, 1.0)) == (0.0, 0.0)
    out = run(d, clock, 1.0, refresh=(0, 1, 0.5))
    assert out == pytest.approx((0.2, 0.2))                        # 50 % de la velocidad, con tope 0.4


def test_giro_en_el_sitio():
    d, _, clock, _ = make()
    out = run(d, clock, 1.0, refresh=(1, 0, 1.0))
    assert out == pytest.approx((0.4, -0.4))


def test_invertir_el_sentido_pasa_por_cero():
    d, m, clock, _ = make()
    run(d, clock, 1.0, refresh=(0, 1, 1.0))
    d.command(0, -1)
    signs = []
    for _ in range(30):
        clock.advance(0.04)
        d.command(0, -1)
        signs.append(d.tick()[0])
    assert signs[0] > 0 and signs[-1] < 0
    assert any(abs(s) < 0.05 for s in signs), "no debe saltar de + a - sin pasar por cero"


def test_watchdog_para_de_golpe_y_avisa_una_vez():
    d, m, clock, events = make(watchdog_s=0.5)
    run(d, clock, 1.0, refresh=(0, 1, 1.0))
    assert m.left > 0
    run(d, clock, 0.4)                                            # sin órdenes, pero dentro del margen
    assert m.left > 0
    out = run(d, clock, 0.4)                                      # ya pasó el margen
    assert out == (0.0, 0.0) and m.left == 0 and m.stops > 0
    assert d.status()["stale"] is True
    run(d, clock, 1.0)
    assert events.count("watchdog") == 1
    # una orden nueva reanuda
    assert run(d, clock, 1.0, refresh=(0, 1, 1.0)) == pytest.approx((0.4, 0.4))
    assert d.status()["stale"] is False


def test_paro_de_emergencia_bloquea_hasta_reanudar():
    d, m, clock, events = make()
    run(d, clock, 1.0, refresh=(0, 1, 1.0))
    d.estop()
    assert (m.left, m.right) == (0, 0) and d.status()["estop"] is True
    assert d.command(0, 1) is False
    assert run(d, clock, 1.0, refresh=(0, 1, 1.0)) == (0.0, 0.0), "ninguna orden mueve el robot en paro"
    assert d.reset_estop() is True
    assert d.status()["estop"] is False
    assert (m.left, m.right) == (0, 0), "al reanudar sigue parado hasta que haya una orden"
    assert run(d, clock, 1.0, refresh=(0, 1, 1.0)) == pytest.approx((0.4, 0.4))
    assert events.count("estop") == 1 and events.count("estop_reset") == 1


def test_estop_repetido_no_repite_el_evento():
    d, _, _, events = make()
    d.estop(); d.estop()
    assert events == ["estop"]


def test_obstaculo_al_frente_frena_de_golpe_y_no_bloquea_el_giro():
    d, m, clock, events = make()
    run(d, clock, 1.0, refresh=(0, 1, 1.0))
    d.set_obstacles(front_cm=12, rear_cm=None)
    clock.advance(0.04)
    d.command(0, 1)
    out = d.tick()
    assert out == (0.0, 0.0), "por debajo del mínimo el avance se anula en el acto"
    assert d.status()["limited"] == "obstaculo_frente"
    out = run(d, clock, 0.6, refresh=(0.8, 0.5, 1.0), obstacles=(12, None))   # girar hacia la derecha sí se permite
    assert out[0] > 0 > out[1]
    assert events.count("obstaculo_frente") == 1


def test_obstaculo_cerca_reduce_sin_cortar():
    d, _, clock, events = make()
    out = run(d, clock, 1.5, refresh=(0, 1, 1.0), obstacles=(27.5, None))
    assert out == pytest.approx((0.2, 0.2))
    assert events == []


def test_reversa_bloqueada_por_el_sensor_trasero():
    d, _, clock, events = make()
    assert run(d, clock, 1.0, refresh=(0, -1, 1.0), obstacles=(None, 10)) == (0.0, 0.0)
    assert "obstaculo_atras" in events


def test_obstaculo_despejado_permite_avanzar_de_nuevo():
    d, _, clock, _ = make()
    assert run(d, clock, 0.5, refresh=(0, 1, 1.0), obstacles=(10, None)) == (0.0, 0.0)
    assert run(d, clock, 1.0, refresh=(0, 1, 1.0), obstacles=(200, None)) == pytest.approx((0.4, 0.4))


def test_sin_datos_de_los_sensores_se_bloquea_avance_y_reversa():
    """Fail-safe: si el servicio de ToF muere, el último dato "despejado" no puede valer para siempre."""
    d, _, clock, events = make()
    assert run(d, clock, 1.0, refresh=(0, 1, 1.0), obstacles=(None, None)) == pytest.approx((0.4, 0.4))
    out = run(d, clock, 1.0, refresh=(0, 1, 1.0))                # los sensores dejan de reportar
    assert out == (0.0, 0.0)
    assert d.status()["obstacles_stale"] is True and d.status()["limited"] == "obstaculo_frente"
    assert run(d, clock, 1.0, refresh=(0, -1, 1.0)) == (0.0, 0.0), "tampoco se puede retroceder a ciegas"
    assert "obstaculo_frente" in events
    # al volver los datos se recupera solo
    assert run(d, clock, 1.0, refresh=(0, 1, 1.0), obstacles=(None, None)) == pytest.approx((0.4, 0.4))
    assert d.status()["obstacles_stale"] is False


def test_sin_servicio_de_sensores_no_se_bloquea_nada():
    """Sin ToF configurados nadie llama a set_obstacles: el robot se maneja como antes (con o sin sensores)."""
    d, _, clock, _ = make()
    assert run(d, clock, 2.0, refresh=(0, 1, 1.0)) == pytest.approx((0.4, 0.4))
    assert d.status()["obstacles_stale"] is False


def test_el_giro_sigue_permitido_con_los_sensores_caidos():
    d, _, clock, _ = make()
    run(d, clock, 0.2, obstacles=(None, None))
    out = run(d, clock, 1.0, refresh=(1, 0, 1.0))                # giro en el sitio, sin datos de sensores
    assert out[0] > 0 > out[1]


@pytest.mark.parametrize("args", [(float("nan"), 0), (0, float("inf")), (0, 0, float("nan"))])
def test_orden_no_finita_para_y_falla(args):
    d, m, clock, _ = make()
    run(d, clock, 1.0, refresh=(0, 1, 1.0))
    with pytest.raises(ValueError):
        d.command(*args)
    assert (m.left, m.right) == (0, 0)


def test_valores_fuera_de_rango_se_recortan():
    d, _, clock, _ = make()
    out = run(d, clock, 1.0, refresh=(0, 50, 9))
    assert out == pytest.approx((0.4, 0.4))


def test_estado_inicial_sin_ordenes_es_parado():
    d, m, clock, _ = make()
    run(d, clock, 2.0)
    assert (m.left, m.right) == (0, 0)


def test_orden_directa_por_oruga():
    d, _, clock, _ = make()
    d.command_tracks(1.0, 0.5)
    for _ in range(25):
        clock.advance(0.04)
        d.command_tracks(1.0, 0.5)
        out = d.tick()
    assert out == pytest.approx((0.4, 0.2))


def test_cerrar_para_y_libera_el_hardware():
    d, m, _, _ = make()
    d.command(0, 1)
    d.close()
    assert m.closed and (m.left, m.right) == (0, 0)


class ExplodingMotors(FakeMotors):
    def set_speeds(self, left, right):
        raise RuntimeError("fallo de GPIO simulado")


def test_si_el_control_falla_se_activa_el_paro_de_emergencia():
    """Fail-safe: una excepción en el bucle no puede dejar el robot andando ni el hilo muerto en silencio."""
    motors = ExplodingMotors()
    d = Drive(motors, MotorsCfg(), SafetyCfg())
    d.command(0, 1)
    d.start()
    deadline = time.time() + 3
    while not d.status()["estop"] and time.time() < deadline:
        time.sleep(0.02)
    d.close()
    assert d.status()["estop"] is True and motors.stops > 0
