"""Detector de personas, gatos y perros: EfficientDet-Lite0 (COCO) en TFLite, con NMS incluido en el modelo.

La lógica de interpretar las salidas (`parse_outputs`) es pura, así que se prueba sin modelo. Sobre una Pi 4
la inferencia tarda ~95 ms con 3 hilos (~10 fps de tope), y el modelo pesa 4.5 MB.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

import numpy as np

from robok.vision.types import Detection, box_area, intersection

log = logging.getLogger(__name__)

# El modelo entrega el índice COCO de 90 clases empezando en 0 (id COCO - 1): persona 0, gato 16, perro 17.
CLASS_KINDS = {0: "person", 16: "cat", 17: "dog"}
MIN_AREA = 0.004        # cajas menores que esto (0,4 % del cuadro) son ruido para un robot de sala


class Detector(Protocol):
    desc: str

    def detect(self, rgb: np.ndarray) -> list[Detection]: ...


def suppress_containers(dets: list[Detection]) -> list[Detection]:
    """Quita la caja "contenedora" del mismo tipo que abarca a otra de mayor confianza.

    El detector a veces añade, además de los dos gatos reales, una caja de casi todo el cuadro con confianza
    algo menor. Si una caja contiene (≥ 90 % de su área) a otra del mismo tipo con mejor puntuación y es
    claramente más grande, se descarta.
    """
    keep = []
    for d in dets:
        contained = any(
            o is not d and o.kind == d.kind and o.score > d.score and o.area < 0.8 * d.area
            and intersection(o.box, d.box) >= 0.9 * o.area
            for o in dets)
        if not contained:
            keep.append(d)
    return keep


def parse_outputs(boxes, classes, scores, count, kinds: tuple[str, ...], min_score: float,
                  min_area: float = MIN_AREA) -> list[Detection]:
    """Salidas del modelo (cajas ymin,xmin,ymax,xmax normalizadas) -> detecciones de los tipos pedidos."""
    out: list[Detection] = []
    n = int(min(count, len(scores)))
    for i in range(n):
        kind = CLASS_KINDS.get(int(round(float(classes[i]))))
        score = float(scores[i])
        if kind is None or kind not in kinds or score < min_score:
            continue
        ymin, xmin, ymax, xmax = (min(1.0, max(0.0, float(v))) for v in boxes[i])
        box = (xmin, ymin, xmax, ymax)
        if xmax <= xmin or ymax <= ymin or box_area(box) < min_area:
            continue
        out.append(Detection(kind, score, box))
    out = suppress_containers(out)
    return sorted(out, key=lambda d: -d.score)


class TfliteDetector:
    def __init__(self, model_path: str | Path, kinds: tuple[str, ...], min_score: float, threads: int = 2):
        import cv2  # noqa: PLC0415  (import tardío: solo el robot real lo necesita)
        from ai_edge_litert.interpreter import Interpreter  # noqa: PLC0415

        self._cv2 = cv2
        self.kinds, self.min_score = tuple(kinds), min_score
        self._it = Interpreter(model_path=str(model_path), num_threads=threads)
        self._it.allocate_tensors()
        inp = self._it.get_input_details()[0]
        self._in, (self._h, self._w) = inp["index"], (int(inp["shape"][1]), int(inp["shape"][2]))
        if inp["dtype"] != np.uint8:
            raise ValueError(f"se esperaba un modelo con entrada uint8 (con NMS incluido), no {inp['dtype'].__name__}")
        outs = self._it.get_output_details()
        shapes = [tuple(int(v) for v in o["shape"]) for o in outs]
        if len(outs) != 4 or len(shapes[0]) != 3 or shapes[0][2] != 4:
            raise ValueError(f"el modelo no trae NMS incluido (salidas {shapes}); usa efficientdet_lite0_detection")
        # orden estándar de TFLite_Detection_PostProcess: cajas, clases, puntuaciones, cantidad
        self._out = [o["index"] for o in outs]
        self.desc = f"EfficientDet-Lite0 {self._w}x{self._h} ({threads} hilos)"

    def detect(self, rgb: np.ndarray) -> list[Detection]:
        x = self._cv2.resize(rgb, (self._w, self._h), interpolation=self._cv2.INTER_LINEAR)
        self._it.set_tensor(self._in, np.ascontiguousarray(x[None], dtype=np.uint8))
        self._it.invoke()
        boxes, classes, scores, count = (self._it.get_tensor(i) for i in self._out)
        return parse_outputs(boxes[0], classes[0], scores[0], int(count[0]), self.kinds, self.min_score)
