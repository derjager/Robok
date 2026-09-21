import random

import pytest
from PIL import ImageChops

from robok.hal.display import FakeDisplay
from robok.services.face import EXPRESSIONS, Face, render


def differ(a, b) -> bool:
    return ImageChops.difference(a, b).getbbox() is not None


@pytest.mark.parametrize("name", list(EXPRESSIONS))
def test_cada_expresion_se_dibuja_del_tamano_pedido(name):
    img = render(name, size=(240, 320))
    assert img.size == (240, 320) and img.mode == "RGB"
    assert img.getbbox() is not None, "la cara no puede quedar toda negra"


def test_las_expresiones_son_distintas_entre_si():
    imgs = {n: render(n) for n in EXPRESSIONS}
    names = list(imgs)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            assert differ(imgs[a], imgs[b]), f"{a} y {b} se ven igual"


def test_parpadeo_y_mirada_cambian_la_imagen():
    base = render("neutral")
    assert differ(base, render("neutral", blink=1.0))
    assert differ(base, render("neutral", gaze=(1.0, 0.0)))
    assert differ(render("neutral", gaze=(-1, 0)), render("neutral", gaze=(1, 0)))


def test_funciona_tambien_apaisada():
    assert render("happy", size=(320, 240)).size == (320, 240)


def test_expresion_desconocida_cae_a_neutral():
    assert not differ(render("no-existe"), render("neutral"))


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def make_face():
    clock = Clock()
    display = FakeDisplay()
    return Face(display, fps=20, rng=random.Random(1), clock=clock), display, clock


def test_tick_solo_dibuja_si_algo_cambio():
    face, display, clock = make_face()
    assert face.tick() is True          # primer cuadro
    assert face.tick() is False         # nada cambió: no se manda al SPI
    assert display.frames == 1
    face.set_expression("happy")
    assert face.tick() is True and display.frames == 2


def test_expresion_invalida_lanza_error_claro():
    face, _, _ = make_face()
    with pytest.raises(ValueError, match="desconocida"):
        face.set_expression("furioso")


def test_hold_vuelve_a_la_expresion_base():
    face, _, clock = make_face()
    face.set_expression("sad")
    face.set_expression("surprised", hold=2.0)
    face.tick()
    assert face.state()["expression"] == "surprised"
    clock.t += 2.5
    face.tick()
    assert face.state()["expression"] == "sad"


def test_parpadea_solo_y_termina():
    face, display, clock = make_face()
    face.tick()
    frames = display.frames
    clock.t += 10                      # ya toca parpadear
    seen = set()
    for _ in range(12):
        face.tick()
        seen.add(face._last_key[3])
        clock.t += 0.03
    assert 1.0 in seen and 0.0 in seen  # cerró y volvió a abrir
    assert display.frames > frames


def test_la_mirada_se_suaviza_hacia_el_objetivo():
    face, _, _ = make_face()
    face.look(1.0, 0.0)
    xs = []
    for _ in range(6):
        face.tick()
        xs.append(face.state()["gaze"][0])
    assert xs == sorted(xs) and xs[-1] > 0.9 and xs[0] < xs[-1]


def test_mirada_se_limita_a_menos_uno_y_uno():
    face, _, _ = make_face()
    face.look(50, -50)
    for _ in range(20):
        face.tick()
    gx, gy = face.state()["gaze"]
    assert gx <= 1.0 and gy >= -1.0


def test_frame_devuelve_una_imagen_aunque_no_haya_tick():
    face, display, _ = make_face()
    assert face.frame().size == display.size


def test_hilo_de_la_cara_arranca_y_se_detiene():
    display = FakeDisplay()
    face = Face(display, fps=50)
    face.start()
    face.set_expression("love")
    import time
    deadline = time.time() + 2
    while display.frames < 2 and time.time() < deadline:
        time.sleep(0.02)
    face.stop()
    assert display.frames >= 2
