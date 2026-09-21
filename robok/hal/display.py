"""LCD: framebuffer real (/dev/fbN del driver fb_ili9340) y versión simulada."""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Protocol

import numpy as np
from PIL import Image

log = logging.getLogger(__name__)

SYSFS_GRAPHICS = Path("/sys/class/graphics")
DEFAULT_SIZE = (240, 320)  # vertical, rotate=0 (ver PINOUT.md §4.1)


class DisplayNotFound(RuntimeError):
    pass


class Display(Protocol):
    size: tuple[int, int]

    def show(self, img: Image.Image) -> None: ...
    def close(self) -> None: ...


def rgb565_bytes(img: Image.Image) -> bytes:
    """Convierte una imagen a RGB565 little-endian, el formato del framebuffer."""
    a = np.asarray(img.convert("RGB"), dtype=np.uint16)
    v = ((a[..., 0] & 0xF8) << 8) | ((a[..., 1] & 0xFC) << 3) | (a[..., 2] >> 3)
    return v.astype("<u2").tobytes()


def find_framebuffer(name: str, sysfs: Path = SYSFS_GRAPHICS) -> tuple[str, tuple[int, int]]:
    """Busca el framebuffer por nombre de driver (no por número: fb0 es el HDMI)."""
    for d in sorted(sysfs.glob("fb[0-9]*")):
        try:
            if (d / "name").read_text().strip() != name:
                continue
            w, h = (int(x) for x in (d / "virtual_size").read_text().split(","))
            bpp = int((d / "bits_per_pixel").read_text())
        except (OSError, ValueError):
            continue
        if bpp != 16:
            raise DisplayNotFound(f"{d.name} usa {bpp} bpp; se esperaba RGB565 (16 bpp)")
        return f"/dev/{d.name}", (w, h)
    raise DisplayNotFound(f"no hay framebuffer '{name}'. ¿Está cargado el overlay del LCD?")


class FramebufferDisplay:
    def __init__(self, name: str = "fb_ili9340"):
        self.path, self.size = find_framebuffer(name)
        try:
            self._fd = os.open(self.path, os.O_WRONLY)
        except PermissionError as e:
            raise DisplayNotFound(f"sin permiso en {self.path}: el usuario debe estar en el grupo 'video'") from e
        self._lock = threading.Lock()
        log.info("LCD %s %dx%d", self.path, *self.size)

    def show(self, img: Image.Image) -> None:
        if img.size != self.size:
            img = img.resize(self.size)
        data = rgb565_bytes(img)
        with self._lock:
            if self._fd >= 0:
                os.pwrite(self._fd, data, 0)

    def close(self) -> None:
        with self._lock:
            if self._fd >= 0:
                os.close(self._fd)
                self._fd = -1


class FakeDisplay:
    """Guarda el último cuadro en memoria. Sirve para pruebas y para el modo simulado."""

    def __init__(self, size: tuple[int, int] = DEFAULT_SIZE):
        self.size = size
        self.frames = 0
        self.last: Image.Image | None = None
        self._lock = threading.Lock()

    def show(self, img: Image.Image) -> None:
        with self._lock:
            self.frames += 1
            self.last = img.copy()

    def close(self) -> None:
        pass


def create_display(name: str, sim: bool) -> tuple[Display, str]:
    """Devuelve (display, descripción). Si el LCD real falla, cae al simulado y lo avisa."""
    if sim:
        return FakeDisplay(), "simulado"
    try:
        d = FramebufferDisplay(name)
        return d, f"{d.path} {d.size[0]}x{d.size[1]}"
    except DisplayNotFound as e:
        log.warning("LCD no disponible (%s); se usa una pantalla simulada", e)
        return FakeDisplay(), f"simulado (LCD no disponible: {e})"
