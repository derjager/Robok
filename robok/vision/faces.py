"""Caras: YuNet las encuentra y SFace saca su huella de 128 números (ambos de OpenCV Zoo)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from robok.vision.embedder import normalize

MAX_SIDE = 320          # YuNet cuesta según los píxeles: se le da el recorte reducido a este lado mayor
MIN_FACE_PX = 24        # una cara más chica que esto (en el original) no da una huella fiable


@dataclass(frozen=True)
class Face:
    embedding: np.ndarray       # 128, normalizada
    box: tuple[float, float, float, float]      # x0, y0, x1, y1 relativos al recorte (0..1)
    score: float                # confianza de YuNet
    px: int                     # lado menor de la cara, en píxeles del recorte original


class FaceRecognizer(Protocol):
    def extract(self, rgb_crop: np.ndarray) -> Face | None: ...


class OpenCvFaces:
    def __init__(self, detector_path: str | Path, recognizer_path: str | Path, min_score: float = 0.7):
        import cv2  # noqa: PLC0415

        self._cv2 = cv2
        self._det = cv2.FaceDetectorYN.create(str(detector_path), "", (MAX_SIDE, MAX_SIDE), min_score, 0.3, 5)
        self._rec = cv2.FaceRecognizerSF.create(str(recognizer_path), "")
        self.desc = "YuNet + SFace"

    def extract(self, rgb_crop: np.ndarray) -> Face | None:
        cv2 = self._cv2
        bgr = cv2.cvtColor(rgb_crop, cv2.COLOR_RGB2BGR)
        h, w = bgr.shape[:2]
        s = min(1.0, MAX_SIDE / max(h, w))
        small = cv2.resize(bgr, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else bgr
        self._det.setInputSize((small.shape[1], small.shape[0]))
        _, faces = self._det.detect(small)
        if faces is None or len(faces) == 0:
            return None
        f = faces[int(np.argmax(faces[:, -1]))].copy()
        f[:14] /= s                                    # de vuelta a las coordenadas del recorte original
        fw, fh = float(f[2]), float(f[3])
        if min(fw, fh) < MIN_FACE_PX:
            return None
        feat = self._rec.feature(self._rec.alignCrop(bgr, f))
        x0, y0 = max(0.0, float(f[0]) / w), max(0.0, float(f[1]) / h)
        return Face(normalize(feat), (x0, y0, min(1.0, x0 + fw / w), min(1.0, y0 + fh / h)), float(f[-1]),
                    int(min(fw, fh)))
