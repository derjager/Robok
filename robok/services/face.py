"""La cara de Wally: expresiones dibujadas con Pillow y animación (parpadeo y mirada).

`render()` es una función pura (fácil de probar). `Face` es el servicio que la anima en un hilo
y empuja los cuadros al display solo cuando algo cambió, para no saturar el SPI (~13 fps máx).
El tamaño se lee del display, no se fija: está pensada para vertical 240x320.
"""
from __future__ import annotations

import logging
import math
import random
import threading
import time
from dataclasses import dataclass
from typing import Callable

from PIL import Image, ImageDraw

from robok.hal.display import Display

log = logging.getLogger(__name__)

SS = 3  # supermuestreo para bordes suaves
BG = (0, 0, 0)


@dataclass(frozen=True)
class Expr:
    color: tuple[int, int, int]
    eyes: str             # oval | arc | heart | round | line
    open: float           # apertura de los ojos, 0..1 (solo oval/round)
    brow: float           # >0: cejas de enfado (interior abajo); <0: de tristeza; 0: sin cejas
    mouth: str            # smile | grin | frown | flat | open | small
    curve: float = 0.25   # curvatura de la boca respecto a su ancho


EXPRESSIONS: dict[str, Expr] = {
    "neutral":   Expr((0, 220, 255), "oval", 1.0, 0.0, "smile", 0.22),
    "happy":     Expr((120, 255, 140), "arc", 1.0, 0.0, "grin", 0.30),
    "sad":       Expr((110, 150, 255), "oval", 0.85, -1.0, "frown", 0.22),
    "surprised": Expr((255, 240, 120), "round", 1.0, 0.0, "open", 0.34),
    "sleepy":    Expr((150, 170, 200), "oval", 0.28, 0.0, "flat", 0.0),
    "angry":     Expr((255, 90, 70), "oval", 0.72, 1.0, "frown", 0.16),
    "love":      Expr((255, 100, 170), "heart", 1.0, 0.0, "grin", 0.28),
    "thinking":  Expr((200, 160, 255), "oval", 0.85, -0.4, "small", 0.0),
}
DEFAULT_EXPRESSION = "neutral"


def _heart(cx: float, cy: float, size: float, n: int = 72) -> list[tuple[float, float]]:
    pts = []
    for i in range(n):
        t = 2 * math.pi * i / n
        x = 16 * math.sin(t) ** 3
        y = 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)
        pts.append((cx + x * size / 34, cy - y * size / 34))
    return pts


def render(expression: str = DEFAULT_EXPRESSION, gaze: tuple[float, float] = (0.0, 0.0),
           blink: float = 0.0, size: tuple[int, int] = (240, 320)) -> Image.Image:
    """Dibuja la cara. gaze en [-1, 1] (x derecha, y abajo); blink 0 (abierto) a 1 (cerrado)."""
    ex = EXPRESSIONS.get(expression) or EXPRESSIONS[DEFAULT_EXPRESSION]
    w, h = size
    W, H = w * SS, h * SS
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    gx = max(-1.0, min(1.0, gaze[0]))
    gy = max(-1.0, min(1.0, gaze[1]))

    unit = min(w, h * 0.75)                 # escala respecto al lado corto útil
    ew = 0.33 * unit * SS                    # ancho del ojo
    eh = ew * 1.25                           # alto del ojo
    ecy = 0.36 * H + gy * 0.02 * H
    sep = 0.235 * W
    line_w = max(2, int(0.045 * unit * SS))

    for side in (-1, 1):
        cx = W / 2 + side * sep + gx * 0.035 * W
        _draw_eye(d, ex, cx, ecy, ew, eh, gx, gy, blink, line_w)
        if ex.brow:
            _draw_brow(d, ex, side, cx, ecy - eh / 2 - 0.05 * H, ew, line_w)

    _draw_mouth(d, ex, W / 2 + gx * 0.02 * W, 0.70 * H + gy * 0.015 * H, 0.40 * unit * SS, line_w)
    return img.resize(size, Image.LANCZOS)


def _draw_eye(d, ex: Expr, cx, cy, ew, eh, gx, gy, blink, line_w) -> None:
    col = ex.color
    if ex.eyes == "arc":   # ojos felices ^ ^
        box = (cx - ew * 0.55, cy - ew * 0.30, cx + ew * 0.55, cy + ew * 0.80)
        d.arc(box, 205, 335, fill=col, width=int(line_w * 1.5))
        return
    if ex.eyes == "heart":
        k = 1.0 - 0.85 * blink
        pts = _heart(cx, cy, ew * 1.15)
        pts = [(x, cy + (y - cy) * k) for x, y in pts]
        d.polygon(pts, fill=col)
        return
    openness = ex.open * (1.0 - 0.94 * blink)
    hh = max(eh * openness, line_w)
    if ex.eyes == "round":
        ew2 = ew * 1.05
        hh = max(ew2 * (1.0 - 0.94 * blink), line_w)
        box = (cx - ew2 / 2, cy - hh / 2, cx + ew2 / 2, cy + hh / 2)
        d.ellipse(box, fill=col)
    else:
        box = (cx - ew / 2, cy - hh / 2, cx + ew / 2, cy + hh / 2)
        d.rounded_rectangle(box, radius=min(ew, hh) / 2, fill=col)
    if hh > ew * 0.55:   # pupila con brillo, sigue la mirada
        pr = ew * 0.20
        px = cx + gx * ew * 0.20
        py = cy + gy * min(hh, eh) * 0.18
        d.ellipse((px - pr, py - pr, px + pr, py + pr), fill=BG)
        hr = pr * 0.34
        d.ellipse((px - pr * 0.55 - hr, py - pr * 0.55 - hr, px - pr * 0.55 + hr, py - pr * 0.55 + hr),
                  fill=(255, 255, 255))


def _draw_brow(d, ex: Expr, side: int, cx, y, ew, line_w) -> None:
    tilt = ex.brow * 0.11 * ew   # >0 enfado (lado interior más bajo), <0 tristeza
    # side=-1 es el ojo izquierdo: su lado interior queda a la derecha (x mayor)
    inner_x, outer_x = cx - side * ew * 0.55, cx + side * ew * 0.55
    p_in, p_out = (inner_x, y + tilt), (outer_x, y - tilt)
    d.line([p_in, p_out], fill=ex.color, width=line_w)
    r = line_w / 2
    for x, yy in (p_in, p_out):
        d.ellipse((x - r, yy - r, x + r, yy + r), fill=ex.color)


def _draw_mouth(d, ex: Expr, cx, cy, mw, line_w) -> None:
    col, mh = ex.color, mw * ex.curve
    box = (cx - mw / 2, cy - mh, cx + mw / 2, cy + mh)
    if ex.mouth == "smile":
        d.arc(box, 25, 155, fill=col, width=line_w)
    elif ex.mouth == "grin":
        d.chord((cx - mw / 2, cy - mh, cx + mw / 2, cy + mh * 1.6), 0, 180, fill=col)
    elif ex.mouth == "frown":
        d.arc((cx - mw / 2, cy, cx + mw / 2, cy + 2 * mh), 205, 335, fill=col, width=line_w)
    elif ex.mouth == "open":
        r = mw * 0.17
        d.ellipse((cx - r, cy - r * 1.2, cx + r, cy + r * 1.2), fill=col)
    elif ex.mouth == "small":
        d.line([(cx - mw * 0.16, cy), (cx + mw * 0.16, cy)], fill=col, width=line_w)
    else:  # flat
        d.line([(cx - mw * 0.30, cy), (cx + mw * 0.30, cy)], fill=col, width=line_w)


# ----------------------------------------------------------------------------------------------
class Face:
    """Anima la cara y la envía al display. Seguro para usar desde varios hilos."""

    BLINK_STEPS = ((0.06, 0.6), (0.12, 1.0), (0.18, 0.6))  # (hasta t, cierre)

    def __init__(self, display: Display, fps: int = 20, rng: random.Random | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.display = display
        self.period = 1.0 / max(1, fps)
        self._rng = rng or random.Random()
        self._clock = clock
        self._lock = threading.RLock()
        self._base = DEFAULT_EXPRESSION
        self._expr = DEFAULT_EXPRESSION
        self._revert_at: float | None = None
        self._gaze_target = (0.0, 0.0)
        self._gaze = (0.0, 0.0)
        self._blink_start: float | None = None
        self._next_blink = self._clock() + self._rng.uniform(2.0, 5.0)
        self._last_key: tuple | None = None
        self._last_frame: Image.Image | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # --- API -------------------------------------------------------------------------------
    def set_expression(self, name: str, hold: float | None = None) -> None:
        """Cambia la expresión. Con `hold` (s) vuelve sola a la expresión base."""
        if name not in EXPRESSIONS:
            raise ValueError(f"expresión desconocida: {name!r}. Válidas: {sorted(EXPRESSIONS)}")
        with self._lock:
            self._expr = name
            if hold:
                self._revert_at = self._clock() + hold
            else:
                self._base, self._revert_at = name, None

    def look(self, x: float, y: float) -> None:
        with self._lock:
            self._gaze_target = (max(-1.0, min(1.0, x)), max(-1.0, min(1.0, y)))

    def blink_now(self) -> None:
        with self._lock:
            self._blink_start = self._clock()

    def state(self) -> dict:
        with self._lock:
            return {"expression": self._expr, "base": self._base,
                    "gaze": [round(self._gaze[0], 2), round(self._gaze[1], 2)]}

    def frame(self) -> Image.Image:
        """Último cuadro dibujado (o uno nuevo si aún no hay)."""
        with self._lock:
            if self._last_frame is None:
                self._last_frame = render(self._expr, self._gaze, 0.0, self.display.size)
            return self._last_frame

    # --- animación -------------------------------------------------------------------------
    def tick(self, now: float | None = None) -> bool:
        """Avanza la animación un paso. Devuelve True si dibujó un cuadro nuevo."""
        now = self._clock() if now is None else now
        with self._lock:
            if self._revert_at is not None and now >= self._revert_at:
                self._expr, self._revert_at = self._base, None
            if self._blink_start is None and now >= self._next_blink:
                self._blink_start = now
            blink = 0.0
            if self._blink_start is not None:
                t = now - self._blink_start
                blink = next((c for limit, c in self.BLINK_STEPS if t < limit), None)
                if blink is None:
                    blink, self._blink_start = 0.0, None
                    self._next_blink = now + self._rng.uniform(2.5, 6.0)
            gx = self._gaze[0] + (self._gaze_target[0] - self._gaze[0]) * 0.5
            gy = self._gaze[1] + (self._gaze_target[1] - self._gaze[1]) * 0.5
            self._gaze = (gx, gy)
            key = (self._expr, round(gx, 2), round(gy, 2), blink)
            if key == self._last_key:
                return False
            self._last_key = key
            expr = self._expr
        img = render(expr, (gx, gy), blink, self.display.size)
        with self._lock:
            self._last_frame = img
        self.display.show(img)
        return True

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="face", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self.tick()
            except Exception:  # la cara nunca debe tumbar al robot
                log.exception("error dibujando la cara")
            self._stop.wait(max(0.0, self.period - (time.monotonic() - t0)))
