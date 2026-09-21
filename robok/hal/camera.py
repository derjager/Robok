"""Cámara: fotogramas JPEG (MJPEG) para la web y, más adelante, para el detector.

La cámara real es una OV5647 por libcamera. En vez de depender de picamera2 (no viene en el venv) se lanza
`rpicam-vid --codec mjpeg` y se leen los JPEG de su salida estándar. La cámara se enciende sola cuando hay
un espectador (`acquire`) y se apaga `idle_s` segundos después de que se va el último (`release`): así no
gasta CPU ni calienta la Pi cuando nadie mira.
"""
from __future__ import annotations

import collections
import logging
import math
import os
import select
import shutil
import subprocess
import threading
import time
from typing import Callable, Iterator, Protocol

from robok.config import CameraCfg

log = logging.getLogger(__name__)

Frame = tuple[int, bytes]          # (número de secuencia, JPEG)
Callback = Callable[[int, bytes], None]
MAX_BUFFER = 8 * 1024 * 1024       # un JPEG mayor que esto no es un fotograma: se descarta
STALL_S = 5.0                      # el proceso vive pero no entrega imágenes: se reinicia
BACKOFF_S = (1.0, 15.0)            # espera mínima y máxima entre reintentos de la cámara real


class Camera(Protocol):
    desc: str

    def acquire(self) -> None: ...
    def release(self) -> None: ...
    def subscribe(self, cb: Callback) -> Callable[[], None]: ...
    def latest(self) -> Frame | None: ...
    def latest_age(self) -> float | None: ...
    def status(self) -> dict: ...
    def close(self) -> None: ...


# --- separador de fotogramas -------------------------------------------------------------------

def _jpeg_end(buf: bytearray) -> int | None:
    """Fin (índice exclusivo) del JPEG que empieza en buf[0] (FFD8), None si aún faltan datos, -1 si está roto.

    Recorre los segmentos por su longitud en vez de buscar FFD9 a ciegas: una tabla de cuantización con
    los valores 0xFF 0xD9 seguidos daría un falso final de imagen.
    """
    n, p = len(buf), 2
    while True:
        if p + 2 > n:
            return None
        if buf[p] != 0xFF:
            return -1
        m = buf[p + 1]
        if m == 0xFF:                            # bytes de relleno
            p += 1
        elif m == 0xD9:                          # EOI
            return p + 2
        elif m == 0x01 or 0xD0 <= m <= 0xD8:     # marcadores sin longitud
            p += 2
        else:
            if p + 4 > n:
                return None
            p += 2 + ((buf[p + 2] << 8) | buf[p + 3])
            if m == 0xDA:                        # SOS: sigue el dato comprimido hasta el próximo marcador real
                while True:
                    q = buf.find(b"\xff", p)
                    if q < 0 or q + 1 >= n:
                        return None
                    nb = buf[q + 1]
                    if nb == 0x00 or 0xD0 <= nb <= 0xD7:    # byte escapado o marcador de reinicio
                        p = q + 2
                    elif nb == 0xFF:
                        p = q + 1
                    else:
                        p = q
                        break


class MjpegSplitter:
    """Parte un flujo de JPEG concatenados (lo que entrega rpicam-vid) en fotogramas completos."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> Iterator[bytes]:
        self._buf += data
        buf = self._buf
        while True:
            i = buf.find(b"\xff\xd8")
            if i < 0:
                del buf[:max(0, len(buf) - 1)]      # conserva un posible FF partido entre lecturas
                return
            if i:
                del buf[:i]
            end = _jpeg_end(buf)
            if end is None:
                if len(buf) > MAX_BUFFER:
                    buf.clear()
                return
            if end < 0:
                del buf[:2]                          # dato corrupto: busca el siguiente inicio
                continue
            frame = bytes(buf[:end])
            del buf[:end]
            yield frame


# --- base común: espectadores, encendido perezoso y reparto de fotogramas ------------------------

class _CameraBase:
    desc = "cámara"

    def __init__(self, cfg: CameraCfg):
        self.cfg = cfg
        self._life = threading.RLock()       # serializa arranque y parada (la cámara real es de uso exclusivo)
        self._viewers = 0
        self._timer: threading.Timer | None = None
        self._run: tuple[threading.Thread, threading.Event] | None = None
        self._subs: list[Callback] = []
        self._latest: Frame | None = None
        self._latest_at = 0.0
        self._times: collections.deque[float] = collections.deque(maxlen=90)   # instantes de los últimos fotogramas
        self._error: str | None = None
        self._closed = False

    # implementado por cada cámara: produce fotogramas con self._publish() hasta que `stop` se active
    def _capture(self, stop: threading.Event) -> None:
        raise NotImplementedError

    def acquire(self) -> None:
        with self._life:
            if self._closed:
                raise RuntimeError("la cámara está cerrada")
            self._viewers += 1
            self._cancel_timer()
            if self._run is None:
                stop = threading.Event()
                t = threading.Thread(target=self._main, args=(stop,), name="camera", daemon=True)
                self._run = (t, stop)
                t.start()

    def release(self) -> None:
        with self._life:
            self._viewers = max(0, self._viewers - 1)
            if self._viewers == 0 and self._run is not None and not self._closed:
                self._cancel_timer()
                self._timer = threading.Timer(self.cfg.idle_s, self._idle_stop)
                self._timer.daemon = True
                self._timer.start()

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _idle_stop(self) -> None:
        with self._life:
            if self._viewers == 0:
                self._stop_capture()

    def _stop_capture(self) -> None:
        with self._life:
            run, self._run = self._run, None
        if run:
            run[1].set()
            run[0].join(timeout=5)
            if run[0].is_alive():
                log.error("el hilo de la cámara no terminó a tiempo")

    def _main(self, stop: threading.Event) -> None:
        try:
            self._capture(stop)
        except Exception as e:
            log.exception("la captura de la cámara falló")
            self._error = f"{type(e).__name__}: {e}"

    def _publish(self, jpeg: bytes) -> None:
        now = time.monotonic()
        self._times.append(now)
        seq = (self._latest[0] + 1) if self._latest else 1
        self._latest, self._latest_at = (seq, jpeg), now
        self._error = None
        for cb in list(self._subs):
            try:
                cb(seq, jpeg)
            except Exception:
                log.exception("suscriptor de la cámara falló; se le da de baja")
                self._unsubscribe(cb)

    def subscribe(self, cb: Callback) -> Callable[[], None]:
        self._subs.append(cb)
        return lambda: self._unsubscribe(cb)

    def _unsubscribe(self, cb: Callback) -> None:
        try:
            self._subs.remove(cb)
        except ValueError:
            pass

    def latest(self) -> Frame | None:
        return self._latest

    def latest_age(self) -> float | None:
        """Segundos desde el último fotograma (None si nunca hubo): permite ignorar uno viejo de una sesión anterior."""
        return None if self._latest is None else time.monotonic() - self._latest_at

    def fps(self, window_s: float = 2.0) -> float:
        """Fotogramas por segundo en los últimos `window_s` segundos (0 si la cámara está parada)."""
        now = time.monotonic()
        recent = [t for t in list(self._times) if now - t <= window_s]
        if len(recent) < 2 or recent[-1] == recent[0]:
            return 0.0
        return (len(recent) - 1) / (recent[-1] - recent[0])

    def status(self) -> dict:
        # sin tomar _life: se llama desde el bucle de eventos y una parada puede tardar unos segundos
        running, viewers = self._run is not None, self._viewers
        fresh = running and self._latest is not None and time.monotonic() - self._latest_at < 2.0
        return {"desc": self.desc, "on": running, "streaming": bool(fresh), "viewers": viewers,
                "fps": round(self.fps(), 1), "width": self.cfg.width, "height": self.cfg.height,
                "error": self._error}

    def close(self) -> None:
        with self._life:
            self._closed = True
            self._cancel_timer()
            self._viewers = 0
        self._stop_capture()
        self._subs.clear()


# --- cámara real -------------------------------------------------------------------------------

class RpicamCamera(_CameraBase):
    def __init__(self, cfg: CameraCfg):
        super().__init__(cfg)
        self.desc = f"OV5647 ({cfg.command} {cfg.width}x{cfg.height}@{cfg.fps})"

    @staticmethod
    def available(command: str = "rpicam-vid") -> bool:
        return shutil.which(command) is not None

    def command(self) -> list[str]:
        c = self.cfg
        cmd = [c.command, "-t", "0", "-n", "--codec", "mjpeg", "--width", str(c.width), "--height", str(c.height),
               "--framerate", str(c.fps), "--quality", str(c.quality), "--flush", "-o", "-"]
        if c.hflip:
            cmd.append("--hflip")
        if c.vflip:
            cmd.append("--vflip")
        return cmd

    def _capture(self, stop: threading.Event) -> None:
        backoff = BACKOFF_S[0]
        while not stop.is_set():
            started = time.monotonic()
            reason = self._run_process(stop)
            if stop.is_set():
                return
            self._error = reason
            log.warning("la cámara se detuvo (%s); reintento en %.0f s", reason, backoff)
            # un proceso que vivió un rato estaba sano: se reinicia pronto; si muere al instante, espera más
            backoff = BACKOFF_S[0] if time.monotonic() - started > 10 else min(backoff * 2, BACKOFF_S[1])
            stop.wait(backoff)

    def _run_process(self, stop: threading.Event) -> str:
        try:
            proc = subprocess.Popen(self.command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        except OSError as e:
            return f"no se pudo lanzar {self.cfg.command}: {e}"
        tail: collections.deque[str] = collections.deque(maxlen=6)
        threading.Thread(target=self._drain, args=(proc, tail), daemon=True, name="camera-stderr").start()
        splitter, last = MjpegSplitter(), time.monotonic()
        fd = proc.stdout.fileno()
        try:
            while not stop.is_set():
                ready, _, _ = select.select([fd], [], [], 0.5)
                if ready:
                    data = os.read(fd, 65536)
                    if not data:
                        return "el proceso terminó: " + " | ".join(tail)[-200:]
                    for frame in splitter.feed(data):
                        last = time.monotonic()
                        self._publish(frame)
                elif proc.poll() is not None:
                    return "el proceso terminó: " + " | ".join(tail)[-200:]
                elif time.monotonic() - last > STALL_S:
                    return f"sin imágenes durante {STALL_S:.0f} s"
            return "detenida"
        finally:
            self._terminate(proc)

    @staticmethod
    def _drain(proc: subprocess.Popen, tail: collections.deque) -> None:
        for line in iter(proc.stderr.readline, b""):
            text = line.decode("utf-8", "replace").strip()
            if text:
                tail.append(text)

    @staticmethod
    def _terminate(proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        for pipe in (proc.stdout, proc.stderr):
            try:
                pipe.close()
            except Exception:
                pass


# --- cámara simulada ---------------------------------------------------------------------------

class FakeCamera(_CameraBase):
    """Genera un patrón animado con la palabra SIMULADA (para --sim y las pruebas)."""

    def __init__(self, cfg: CameraCfg, why: str = "simulada", scene: bool = False):
        super().__init__(cfg)
        self.desc = why
        self.scene = scene           # dibuja además la escena de robok.vision.sim (gatos y persona de mentira)
        self.frames = 0

    def _draw(self, i: int) -> bytes:
        import io  # noqa: PLC0415

        from PIL import Image, ImageDraw  # noqa: PLC0415

        w, h = self.cfg.width, self.cfg.height
        img = Image.new("RGB", (w, h), (24, 32, 44))
        d = ImageDraw.Draw(img)
        for x in range(0, w, 40):
            d.line([(x, 0), (x, h)], fill=(40, 52, 68))
        for y in range(0, h, 40):
            d.line([(0, y), (w, y)], fill=(40, 52, 68))
        cx = w / 2 + math.sin(i / 8) * w * 0.3
        cy = h / 2 + math.cos(i / 11) * h * 0.25
        r = min(w, h) * 0.08
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(46, 196, 230))
        if self.scene:
            from robok.vision.sim import draw_scene  # noqa: PLC0415

            draw_scene(img, time.monotonic())
        d.text((10, 10), f"CAMARA SIMULADA  #{i}", fill=(255, 216, 102))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=self.cfg.quality)
        return buf.getvalue()

    def _capture(self, stop: threading.Event) -> None:
        period = 1.0 / self.cfg.fps
        while not stop.is_set():
            t0 = time.monotonic()
            self.frames += 1
            self._publish(self._draw(self.frames))
            stop.wait(max(0.0, period - (time.monotonic() - t0)))


def create_camera(cfg: CameraCfg, sim: bool, scene: bool = False) -> Camera | None:
    """None si la cámara está desactivada; simulada con --sim o si falta rpicam-vid (la web lo avisa)."""
    if not cfg.enabled:
        return None
    if sim:
        return FakeCamera(cfg, "simulada (--sim)", scene=scene)
    if not RpicamCamera.available(cfg.command):
        log.warning("%s no está instalado; la cámara queda simulada", cfg.command)
        return FakeCamera(cfg, f"simulada (falta {cfg.command})")
    return RpicamCamera(cfg)
