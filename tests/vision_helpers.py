"""Cuadros sintéticos para probar la visión con el detector/embedder simulados (robok.vision.sim)."""
import io

from PIL import Image, ImageDraw

from robok.vision.sim import BLUE, GRAY, ORANGE, SKIN

BG = (24, 32, 44)
W, H = 320, 240


def frame(cats=(), person=None, gray=None, patch=0.0, skin=True, size=(W, H)) -> bytes:
    """JPEG con: `cats` = cajas (x0, y0, x1, y1) normalizadas de gatos naranjas, `gray` = caja de un gato gris,
    `person` = caja de una persona (con su parche de piel salvo skin=False), `patch` = fracción de blanco en el gato
    naranja (cambia su huella entre muestras)."""
    w, h = size
    img = Image.new("RGB", size, BG)
    d = ImageDraw.Draw(img)

    def px(b):
        return (b[0] * w, b[1] * h, b[2] * w - 1, b[3] * h - 1)
    for b in cats:
        x0, y0, x1, y1 = px(b)
        d.rectangle((x0, y0, x1, y1), fill=ORANGE)
        if patch:                                      # mancha blanca CENTRADA (dentro de la caja del gato)
            k = patch ** 0.5 / 2
            cx, cy, hw, hh = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) * k, (y1 - y0) * k
            d.rectangle((cx - hw, cy - hh, cx + hw, cy + hh), fill=(255, 255, 255))
    if gray:
        d.rectangle(px(gray), fill=GRAY)
    if person:
        x0, y0, x1, y1 = px(person)
        d.rectangle((x0, y0, x1, y1), fill=BLUE)
        if skin:
            d.rectangle((x0 + (x1 - x0) * .25, y0 + 6, x0 + (x1 - x0) * .75, y0 + (x1 - x0) * .6), fill=SKIN)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=95, subsampling=0)
    return buf.getvalue()


CAT = (0.40, 0.50, 0.60, 0.75)        # gato al centro
CAT_RIGHT = (0.70, 0.50, 0.90, 0.75)  # gato a la derecha del cuadro
CAT_LEFT = (0.10, 0.50, 0.30, 0.75)
PERSON = (0.70, 0.10, 0.95, 0.90)
GRAYCAT = (0.10, 0.50, 0.30, 0.75)
