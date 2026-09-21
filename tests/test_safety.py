import math

import pytest

from robok.config import SafetyCfg
from robok.services.safety import (brake_slew, deadzone, limit_obstacles, mix_tank, require_finite, slew)

CFG = SafetyCfg()   # stop 20 cm, slow 35 cm, atrás 20 cm


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, "1", None, True])
def test_require_finite_rechaza_lo_no_numerico(bad):
    with pytest.raises(ValueError):
        require_finite(0.5, bad)


def test_require_finite_acepta_numeros():
    require_finite(0, 1, -1, 0.25)


def test_zona_muerta_sin_saltos():
    assert deadzone(0.05, 0.1) == 0 and deadzone(-0.1, 0.1) == 0
    assert deadzone(1.0, 0.1) == pytest.approx(1.0)
    assert deadzone(-1.0, 0.1) == pytest.approx(-1.0)
    assert deadzone(0.1001, 0.1) == pytest.approx(0, abs=1e-3)   # continuo en el borde


def test_mezcla_de_tanque():
    assert mix_tank(0, 1) == (1, 1)             # recto
    assert mix_tank(0, -1) == (-1, -1)          # reversa
    assert mix_tank(1, 0) == (1, -1)            # giro a la derecha en el sitio
    assert mix_tank(-1, 0) == (-1, 1)
    assert mix_tank(0, 0) == (0, 0)


def test_mezcla_conserva_la_proporcion_al_saturar():
    l, r = mix_tank(0.8, 0.8)                   # 1.6 y 0.0
    assert (l, r) == (1.0, 0.0)
    l, r = mix_tank(0.6, 0.9)                   # 1.5 y 0.3 -> escala 1/1.5
    assert l == pytest.approx(1.0) and r == pytest.approx(0.2)
    assert max(abs(l), abs(r)) <= 1.0


def test_sin_lecturas_no_hay_limite():
    assert limit_obstacles(0.8, 0.8, None, None, CFG) == (0.8, 0.8, None)


def test_frente_lejos_no_limita():
    assert limit_obstacles(0.8, 0.8, 100, None, CFG) == (0.8, 0.8, None)


def test_frente_cerca_reduce_linealmente():
    l, r, why = limit_obstacles(1.0, 1.0, 27.5, None, CFG)   # a mitad entre 20 y 35
    assert l == pytest.approx(0.5) and r == pytest.approx(0.5) and why == "obstaculo_frente_cerca"


@pytest.mark.parametrize("dist", [20, 10, 0])
def test_frente_en_el_limite_anula_el_avance(dist):
    l, r, why = limit_obstacles(1.0, 1.0, dist, None, CFG)
    assert (l, r) == (0.0, 0.0) and why == "obstaculo_frente"


def test_el_giro_se_conserva_junto_a_un_obstaculo():
    # avanza a 0.8 girando: izquierda 1.0, derecha 0.6 -> sin avance queda el giro puro
    l, r, why = limit_obstacles(1.0, 0.6, 10, None, CFG)
    assert l == pytest.approx(0.2) and r == pytest.approx(-0.2) and why == "obstaculo_frente"


def test_giro_en_el_sitio_nunca_se_limita():
    assert limit_obstacles(0.7, -0.7, 5, 5, CFG) == (0.7, -0.7, None)


def test_reversa_bloqueada_solo_por_el_sensor_trasero():
    assert limit_obstacles(-0.8, -0.8, 5, 100, CFG) == (-0.8, -0.8, None)      # frente cerca no afecta
    l, r, why = limit_obstacles(-0.8, -0.8, 100, 15, CFG)
    assert (l, r) == (0.0, 0.0) and why == "obstaculo_atras"


def test_avanzar_no_lo_frena_el_sensor_trasero():
    assert limit_obstacles(0.8, 0.8, 100, 1, CFG)[2] is None


def test_slew():
    assert slew(0.0, 1.0, 0.1) == pytest.approx(0.1)
    assert slew(0.95, 1.0, 0.1) == 1.0
    assert slew(0.0, -1.0, 0.25) == -0.25


def test_brake_slew_frena_de_golpe_pero_no_invierte_de_golpe():
    assert brake_slew(0.6, 0.0, 0.05) == 0.0            # parar: inmediato
    assert brake_slew(0.6, 0.2, 0.05) == 0.2            # reducir: inmediato
    assert brake_slew(0.6, -0.3, 0.05) == 0.0           # invertir: primero a cero
    assert brake_slew(-0.6, 0.3, 0.05) == 0.0
    assert brake_slew(0.0, -0.3, 0.05) == pytest.approx(-0.05)   # y luego acelera por rampa
    assert brake_slew(0.2, 0.6, 0.05) == pytest.approx(0.25)     # acelerar sigue con rampa
