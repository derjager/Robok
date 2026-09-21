"""Humo con los modelos REALES. Se omite si no están descargados (scripts/get_models.py)."""
import numpy as np
import pytest

from robok.config import VisionCfg
from robok.vision import models

CFG = VisionCfg()
pytestmark = pytest.mark.skipif(bool(models.missing(CFG)), reason="faltan los modelos: corre scripts/get_models.py")


@pytest.fixture(autouse=True)
def _rutas_reales(monkeypatch):
    """Estas pruebas leen models/ de verdad (el resto de las pruebas redirige las rutas a un tmp)."""
    import robok.core as core
    monkeypatch.setattr(core, "resolve_dir", models.resolve_dir)


@pytest.fixture(scope="module")
def backends():
    return models.load_backends(CFG)


def test_los_archivos_coinciden_con_los_hashes_registrados():
    for m in models.MODELS:
        assert models.sha256_of(models.models_path(CFG) / m.filename) == m.sha256, m.filename


def test_el_detector_no_ve_nada_en_una_imagen_vacia_y_no_falla_con_ruido(backends):
    det = backends[0]
    assert det.detect(np.zeros((480, 640, 3), np.uint8)) == []
    noise = np.random.default_rng(1).integers(0, 255, (480, 640, 3), dtype=np.uint8)
    assert isinstance(det.detect(noise), list)


def test_la_huella_de_gato_es_unitaria_estable_y_distingue_imagenes(backends):
    emb = backends[1]
    rng = np.random.default_rng(2)
    a = rng.integers(0, 255, (120, 90, 3), dtype=np.uint8)
    b = np.full((120, 90, 3), (200, 120, 40), np.uint8)
    va, vb = emb.embed(a), emb.embed(b)
    assert va.shape == (1024,) and np.linalg.norm(va) == pytest.approx(1.0, abs=1e-4)
    assert float(va @ emb.embed(a)) == pytest.approx(1.0, abs=1e-4), "determinista"
    assert float(va @ vb) < 0.95


def test_sin_cara_no_hay_huella_de_persona(backends):
    faces = backends[2]
    assert faces.extract(np.full((240, 180, 3), 128, np.uint8)) is None
