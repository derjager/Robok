"""Tipos comunes de la visión. Las cajas van normalizadas (0..1) como (x0, y0, x1, y1), con y hacia abajo."""
from __future__ import annotations

from dataclasses import dataclass

KIND_ES = {"person": "persona", "cat": "gato", "dog": "perro"}
IDENTIFIABLE = ("person", "cat")        # a estos se les puede enseñar quién es quién

Box = tuple[float, float, float, float]


@dataclass(frozen=True)
class Detection:
    kind: str           # person | cat | dog
    score: float
    box: Box

    @property
    def area(self) -> float:
        return box_area(self.box)

    @property
    def center(self) -> tuple[float, float]:
        return ((self.box[0] + self.box[2]) / 2, (self.box[1] + self.box[3]) / 2)


def box_area(b: Box) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def intersection(a: Box, b: Box) -> float:
    return box_area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))


def iou(a: Box, b: Box) -> float:
    inter = intersection(a, b)
    union = box_area(a) + box_area(b) - inter
    return inter / union if union > 0 else 0.0


def crop(rgb, box: Box, pad: float = 0.0):
    """Recorte de una imagen (alto, ancho, 3) con margen relativo `pad`. Devuelve None si queda vacío."""
    h, w = rgb.shape[:2]
    bw, bh = box[2] - box[0], box[3] - box[1]
    x0 = int(max(0.0, box[0] - pad * bw) * w)
    y0 = int(max(0.0, box[1] - pad * bh) * h)
    x1 = int(min(1.0, box[2] + pad * bw) * w + 0.5)
    y1 = int(min(1.0, box[3] + pad * bh) * h + 0.5)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    return rgb[y0:y1, x0:x1]
