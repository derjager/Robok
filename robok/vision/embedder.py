"""Huella visual de un recorte (para distinguir un gato de otro): MobileNetV3-small de MediaPipe, 1024 números.

Es un modelo genérico de imágenes: no sabe de gatos concretos. Funciona porque huellas de fotos del mismo
gato se parecen mucho más (coseno ≥ 0,8) que las de gatos distintos (≤ 0,6); por eso se enseñan varias
muestras desde distintos ángulos y se exige un margen entre identidades.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

import numpy as np


class Embedder(Protocol):
    def embed(self, rgb_crop: np.ndarray) -> np.ndarray: ...


def normalize(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32).ravel()
    n = float(np.linalg.norm(v))
    if not np.isfinite(n) or n < 1e-9:
        raise ValueError("huella vacía o no válida")
    return v / n


class TfliteEmbedder:
    def __init__(self, model_path: str | Path, threads: int = 2):
        import cv2  # noqa: PLC0415
        from ai_edge_litert.interpreter import Interpreter  # noqa: PLC0415

        self._cv2 = cv2
        self._it = Interpreter(model_path=str(model_path), num_threads=threads)
        self._it.allocate_tensors()
        inp = self._it.get_input_details()[0]
        self._in, self._h, self._w = inp["index"], int(inp["shape"][1]), int(inp["shape"][2])
        self._out = self._it.get_output_details()[0]["index"]
        self.desc = f"MobileNetV3-small {self._w}x{self._h}"

    def embed(self, rgb_crop: np.ndarray) -> np.ndarray:
        x = self._cv2.resize(rgb_crop, (self._w, self._h), interpolation=self._cv2.INTER_AREA)
        self._it.set_tensor(self._in, (x.astype(np.float32) / 255.0)[None])     # entrada en [0, 1] (medido: separa mejor)
        self._it.invoke()
        return normalize(self._it.get_tensor(self._out)[0])
