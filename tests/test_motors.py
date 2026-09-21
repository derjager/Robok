import math

import pytest

from robok.config import MotorsCfg, Pins
from robok.hal.motors import FakeMotors, Tb6612Motors

P = Pins()


class FakeGpio:
    """GPIO falso que recuerda niveles, PWM y el orden exacto de las llamadas."""

    def __init__(self):
        self.levels: dict[int, int] = {}
        self.pwm_state: dict[int, tuple[float, float]] = {}
        self.calls: list[tuple] = []
        self.closed = False

    def claim_output(self, pin, level=0):
        self.levels[pin] = level
        self.calls.append(("claim", pin, level))

    def write(self, pin, level):
        self.pwm_state.pop(pin, None)
        self.levels[pin] = level
        self.calls.append(("write", pin, level))

    def pwm(self, pin, hz, duty):
        if duty <= 0:
            return self.write(pin, 0)
        if duty >= 100:
            return self.write(pin, 1)
        self.pwm_state[pin] = (hz, duty)
        self.calls.append(("pwm", pin, hz, duty))

    def close(self):
        self.closed = True


def make(**cfg):
    g = FakeGpio()
    return Tb6612Motors(g, P, MotorsCfg(**cfg)), g


def test_arranca_en_estado_seguro_con_stby_primero():
    _, g = make()
    claims = [c for c in g.calls if c[0] == "claim"]
    assert claims[0][1] == P.motor_stby, "STBY debe reclamarse primero"
    assert len(claims) == 7 and all(c[2] == 0 for c in claims)
    assert all(v == 0 for v in g.levels.values())


def test_adelante_direccion_y_pwm():
    m, g = make()
    m.set_speeds(0.4, 0.4)
    assert (g.levels[P.motor_ain1], g.levels[P.motor_ain2]) == (1, 0)
    assert (g.levels[P.motor_bin1], g.levels[P.motor_bin2]) == (1, 0)
    assert g.pwm_state[P.motor_pwma][1] == pytest.approx(40)
    assert g.pwm_state[P.motor_pwmb][1] == pytest.approx(40)
    assert g.pwm_state[P.motor_pwma][0] == 1000
    assert g.levels[P.motor_stby] == 1


def test_reversa_invierte_los_in():
    m, g = make()
    m.set_speeds(-0.3, -0.3)
    assert (g.levels[P.motor_ain1], g.levels[P.motor_ain2]) == (0, 1)
    assert (g.levels[P.motor_bin1], g.levels[P.motor_bin2]) == (0, 1)


def test_giro_en_el_sitio():
    m, g = make()
    m.set_speeds(0.3, -0.3)
    assert (g.levels[P.motor_ain1], g.levels[P.motor_ain2]) == (1, 0)
    assert (g.levels[P.motor_bin1], g.levels[P.motor_bin2]) == (0, 1)


def test_stby_se_sube_al_final_y_se_baja_primero():
    m, g = make()
    g.calls.clear()
    m.set_speeds(0.5, 0.5)
    assert g.calls[-1] == ("write", P.motor_stby, 1), "STBY sube después de fijar dirección y PWM"
    g.calls.clear()
    m.stop()
    assert g.calls[0] == ("write", P.motor_stby, 0), "STBY baja antes que cualquier otra cosa"
    assert g.levels[P.motor_stby] == 0 and not g.pwm_state
    assert all(g.levels[p] == 0 for p in (P.motor_ain1, P.motor_ain2, P.motor_bin1, P.motor_bin2))


def test_velocidad_cero_equivale_a_parar():
    m, g = make()
    m.set_speeds(0.5, 0.5)
    m.set_speeds(0.0, 0.0)
    assert g.levels[P.motor_stby] == 0 and m.last == (0.0, 0.0)


def test_velocidad_maxima_es_nivel_alto_constante():
    m, g = make()
    m.set_speeds(1.0, 1.0)
    assert g.levels[P.motor_pwma] == 1 and P.motor_pwma not in g.pwm_state


def test_invertir_izquierda():
    m, g = make(invert_left=True)
    m.set_speeds(0.4, 0.4)
    assert (g.levels[P.motor_ain1], g.levels[P.motor_ain2]) == (0, 1)      # izquierda invertida
    assert (g.levels[P.motor_bin1], g.levels[P.motor_bin2]) == (1, 0)      # derecha normal


def test_intercambiar_lados():
    m, g = make(swap_sides=True)
    m.set_speeds(0.4, -0.4)   # "izquierda" ahora es el canal B
    assert (g.levels[P.motor_bin1], g.levels[P.motor_bin2]) == (1, 0)
    assert (g.levels[P.motor_ain1], g.levels[P.motor_ain2]) == (0, 1)


def test_satura_por_encima_de_uno():
    m, g = make()
    m.set_speeds(5.0, -5.0)
    assert m.last == (1.0, -1.0)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_valor_no_finito_para_y_falla(bad):
    m, g = make()
    m.set_speeds(0.5, 0.5)
    with pytest.raises(ValueError):
        m.set_speeds(bad, 0.0)
    assert g.levels[P.motor_stby] == 0


def test_cerrar_deja_todo_en_bajo_libera_y_ya_no_actua():
    m, g = make()
    m.set_speeds(0.5, 0.5)
    m.close()
    assert g.levels[P.motor_stby] == 0 and g.closed
    g.calls.clear()
    m.set_speeds(0.5, 0.5)
    assert g.calls == [] and g.levels[P.motor_stby] == 0
    m.close()   # idempotente


def test_fake_motors_guardan_y_validan():
    f = FakeMotors()
    f.set_speeds(0.2, -0.2)
    assert (f.left, f.right) == (0.2, -0.2) and f.history == [(0.2, -0.2)]
    f.stop()
    assert (f.left, f.right) == (0, 0) and f.stops == 1
    with pytest.raises(ValueError):
        f.set_speeds(math.nan, 0)


# --- inferencia de cableado (puesta en marcha) ---------------------------------------------------
from robok.hal.motors import infer_wiring  # noqa: E402


@pytest.mark.parametrize("obs,expected", [
    (("i", "a", "d", "a"), {"swap_sides": False, "invert_left": False, "invert_right": False}),
    (("i", "r", "d", "a"), {"swap_sides": False, "invert_left": True, "invert_right": False}),
    (("i", "a", "d", "r"), {"swap_sides": False, "invert_left": False, "invert_right": True}),
    (("d", "a", "i", "a"), {"swap_sides": True, "invert_left": False, "invert_right": False}),
    # con lados cruzados: el canal A movió la derecha (hacia atrás) y el B la izquierda (adelante)
    (("d", "r", "i", "a"), {"swap_sides": True, "invert_left": False, "invert_right": True}),
    (("d", "a", "i", "r"), {"swap_sides": True, "invert_left": True, "invert_right": False}),
])
def test_infer_wiring(obs, expected):
    assert infer_wiring(*obs) == expected


@pytest.mark.parametrize("obs", [("i", "a", "i", "a"), ("n", "a", "d", "a"), ("i", "x", "d", "a"),
                                 ("i", "aa", "d", "a"), ("", "a", "d", "a")])
def test_infer_wiring_no_concluyente(obs):
    assert infer_wiring(*obs) is None
