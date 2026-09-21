"""Visión simulada: una escena sintética y un detector/embedder/reconocedor de caras que la interpretan por color.

Sirve para desarrollar y probar el flujo completo (detectar, enseñar quién es quién, seguir con la mirada) sin
cámara ni modelos: la cámara simulada dibuja la escena y estas clases la "ven". Los tests también generan
cuadros con `draw_scene` o con rectángulos a mano.

Escena: un gato naranja que va y viene (y cuya mancha blanca cambia, para que sus muestras no sean idénticas), un gato gris que aparece a ratos, y una persona con un parche de piel.
"""
from __future__ import annotations

import math

import numpy as np

from robok.vision.embedder import normalize
from robok.vision.faces import Face
from robok.vision.types import Box, Detection

ORANGE = (230, 140, 40)
GRAY = (150, 150, 150)
BLUE = (60, 110, 220)
SKIN = (240, 190, 150)
KINDS = (("cat", ORANGE), ("cat", GRAY), ("person", BLUE))
TOL = 24


def scene_boxes(t: float) -> dict[str, Box]:
    """Cajas (x0, y0, x1, y1) normalizadas de cada objeto de la escena en el instante t."""
    cx = 0.5 + 0.3 * math.sin(t / 3.0)
    boxes = {"orange": (cx - 0.11, 0.5, cx + 0.11, 0.72), "person": (0.78, 0.15, 0.96, 0.85)}
    if t % 40 > 20:
        boxes["gray"] = (0.08, 0.5, 0.28, 0.72)
    return boxes


def draw_scene(img, t: float) -> None:
    """Dibuja la escena sobre una imagen PIL."""
    from PIL import ImageDraw  # noqa: PLC0415

    d, (w, h) = ImageDraw.Draw(img), img.size
    for name, color in (("orange", ORANGE), ("gray", GRAY), ("person", BLUE)):
        b = scene_boxes(t).get(name)
        if b is None:
            continue
        px = (b[0] * w, b[1] * h, b[2] * w, b[3] * h)
        d.rectangle(px, fill=color)
        if name == "orange":                     # una mancha blanca que cambia con el tiempo: el "pelaje" se ve distinto al moverse
            k = math.sqrt(0.25 * (1 + math.sin(t * 1.3))) / 2
            cx, cy, hw, hh = (px[0] + px[2]) / 2, (px[1] + px[3]) / 2, (px[2] - px[0]) * k, (px[3] - px[1]) * k
            d.rectangle((cx - hw, cy - hh, cx + hw, cy + hh), fill=(255, 255, 255))
        if name == "person":
            head = (px[0] + (px[2] - px[0]) * 0.25, px[1] + 6, px[0] + (px[2] - px[0]) * 0.75, px[1] + (px[2] - px[0]) * 0.55)
            d.rectangle(head, fill=SKIN)


def _mask(rgb: np.ndarray, color: tuple[int, int, int]) -> np.ndarray:
    return np.all(np.abs(rgb.astype(np.int16) - np.array(color, dtype=np.int16)) <= TOL, axis=-1)


class SimDetector:
    desc = "simulado (por color)"

    def detect(self, rgb: np.ndarray) -> list[Detection]:
        small = rgb[::4, ::4]
        h, w = small.shape[:2]
        out = []
        for kind, color in KINDS:
            ys, xs = np.nonzero(_mask(small, color))
            if len(xs) < 12:
                continue
            out.append(Detection(kind, 0.9, (float(xs.min()) / w, float(ys.min()) / h,
                                             float(xs.max() + 1) / w, float(ys.max() + 1) / h)))
        return out


class SimEmbedder:
    """Huella = qué colores "vivos" hay en el recorte (ignora el fondo oscuro), en un histograma grueso."""

    desc = "simulado (histograma de color)"

    def embed(self, rgb_crop: np.ndarray) -> np.ndarray:
        px = rgb_crop.reshape(-1, 3).astype(np.int16)
        px = px[px.max(axis=1) > 90]                       # fuera el fondo oscuro
        if len(px) == 0:
            raise ValueError("recorte sin color")
        idx = (px // 64).astype(np.int32)                  # 4x4x4 = 64 casillas
        hist = np.bincount(idx[:, 0] * 16 + idx[:, 1] * 4 + idx[:, 2], minlength=64).astype(np.float32)
        return normalize(hist)


class SimFaces:
    desc = "simulado (parche de piel)"

    def extract(self, rgb_crop: np.ndarray) -> Face | None:
        h, w = rgb_crop.shape[:2]
        ys, xs = np.nonzero(_mask(rgb_crop[::2, ::2], SKIN))
        if len(xs) < 8:
            return None
        emb = np.zeros(64, dtype=np.float32)
        emb[(SKIN[0] // 64) * 16 + (SKIN[1] // 64) * 4 + SKIN[2] // 64] = 1.0
        x0, y0, x1, y1 = xs.min() * 2 / w, ys.min() * 2 / h, (xs.max() * 2 + 2) / w, (ys.max() * 2 + 2) / h
        return Face(normalize(emb + 0.02), (float(x0), float(y0), float(x1), float(y1)), 0.9,
                    int(min((x1 - x0) * w, (y1 - y0) * h)))
