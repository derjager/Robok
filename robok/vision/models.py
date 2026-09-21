"""Modelos de la visión: dónde bajarlos, con qué huella SHA-256 y cómo cargarlos.

Los hashes son los de los archivos que se probaron en la Pi (2026-09): si el servidor cambia el archivo, la
descarga se rechaza en vez de ejecutar un modelo distinto del que se validó.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from robok.config import ROOT, VisionCfg

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelFile:
    key: str
    filename: str
    url: str
    sha256: str
    what: str
    licence: str


MODELS = (
    ModelFile("detector", "efficientdet_lite0_detection.tflite",
              "https://storage.googleapis.com/download.tensorflow.org/models/tflite/task_library/object_detection/android/"
              "lite-model_efficientdet_lite0_detection_metadata_1.tflite",
              "2e04c53bfeac0ac2a30c057c7e2a777594ce39baaac35a92f74fb1e8c4fc4e0b",
              "detector de personas, gatos y perros (EfficientDet-Lite0, COCO)", "Apache-2.0"),
    ModelFile("embedder", "mobilenet_v3_small_embedder.tflite",
              "https://storage.googleapis.com/mediapipe-models/image_embedder/mobilenet_v3_small/float32/1/mobilenet_v3_small.tflite",
              "bbbb4c51a55a53905af1daec995ca1aae355046f8839bb8c9f5ce9271394bc40",
              "huella visual para distinguir gatos (MobileNetV3-small, MediaPipe)", "Apache-2.0"),
    ModelFile("face_detector", "face_detection_yunet_2023mar.onnx",
              "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
              "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
              "detector de caras (YuNet, OpenCV Zoo)", "MIT"),
    ModelFile("face_recognizer", "face_recognition_sface_2021dec.onnx",
              "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
              "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
              "huella de caras para distinguir personas (SFace, OpenCV Zoo)", "Apache-2.0"),
)
BY_KEY = {m.key: m for m in MODELS}


def resolve_dir(path: str) -> Path:
    """Ruta de la configuración: relativa a la raíz del repo, o absoluta."""
    p = Path(path)
    return p if p.is_absolute() else ROOT / p


def models_path(cfg: VisionCfg) -> Path:
    return resolve_dir(cfg.models_dir)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def missing(cfg: VisionCfg) -> list[ModelFile]:
    base = models_path(cfg)
    return [m for m in MODELS if not (base / m.filename).is_file()]


def load_backends(cfg: VisionCfg):
    """(detector, embedder, faces, descripción). Lanza si falta algo esencial (el detector)."""
    from robok.vision.detector import TfliteDetector  # noqa: PLC0415
    from robok.vision.embedder import TfliteEmbedder  # noqa: PLC0415
    from robok.vision.faces import OpenCvFaces  # noqa: PLC0415

    base = models_path(cfg)
    have = {m.key for m in MODELS if (base / m.filename).is_file()}
    if "detector" not in have:
        raise FileNotFoundError(f"falta {BY_KEY['detector'].filename} en {base}")
    detector = TfliteDetector(base / BY_KEY["detector"].filename, cfg.kinds, cfg.detect_score, cfg.threads)
    parts = [detector.desc]
    embedder = faces = None
    if "embedder" in have:
        embedder = TfliteEmbedder(base / BY_KEY["embedder"].filename, cfg.threads)
        parts.append("gatos: " + embedder.desc)
    else:
        log.warning("falta %s: no se podrá distinguir un gato de otro", BY_KEY["embedder"].filename)
    if {"face_detector", "face_recognizer"} <= have:
        faces = OpenCvFaces(base / BY_KEY["face_detector"].filename, base / BY_KEY["face_recognizer"].filename)
        parts.append("personas: " + faces.desc)
    else:
        log.warning("faltan los modelos de caras: no se podrá distinguir a una persona de otra")
    return detector, embedder, faces, " · ".join(parts)
