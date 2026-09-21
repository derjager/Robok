import json
import threading
import time

import numpy as np
import pytest

import robok.services.vision as vs
from robok.config import VisionCfg
from robok.services.vision import Vision, VisionError
from robok.vision.gallery import Gallery, GalleryError
from robok.vision.sim import SimDetector, SimEmbedder, SimFaces
from tests.vision_helpers import CAT, CAT_LEFT, CAT_RIGHT, GRAYCAT, PERSON, frame


class StubCamera:
    """Cámara falsa para el bucle: entrega los JPEG que se le pongan y cuenta acquire/release."""

    def __init__(self):
        self.acquired = self.released = 0
        self._latest = None
        self._at = 0.0
        self._seq = 0
        self.desc = "stub"

    def put(self, jpeg):
        self._seq += 1
        self._latest, self._at = (self._seq, jpeg), time.monotonic()

    def acquire(self):
        self.acquired += 1

    def release(self):
        self.released += 1

    def latest(self):
        return self._latest

    def latest_age(self):
        return None if self._latest is None else time.monotonic() - self._at


class Rig:
    def __init__(self, tmp_path, camera=True, real_clock=False, **cfg):
        cfg.setdefault("teach_samples", 4)
        self.cfg = VisionCfg(**cfg)
        self.gallery = Gallery(tmp_path / "g")
        self.looks, self.events = [], []
        self.cam = StubCamera() if camera else None
        self.t = 100.0                                    # reloj propio: los cuadros llevan su hora y el servicio la usa
        self.vision = Vision(self.cfg, self.cam, self.gallery, SimDetector(), SimEmbedder(), SimFaces(),
                             lambda x, y: self.looks.append((round(x, 3), round(y, 3))),
                             settings_path=tmp_path / "settings.json", on_event=self.events.append,
                             clock=time.monotonic if real_clock else (lambda: self.t))

    def step(self, jpeg, dt=0.25, n=1):
        res = None
        for _ in range(n):
            self.t += dt
            res = self.vision.process_frame(jpeg, self.t)
        return res

    def enroll_cat(self, name="Pila", follow=True, n=3):
        """Da de alta un gato naranja directamente en la galería (sin sesión de enseñanza)."""
        ident = self.gallery.create(name, "cat", follow)
        emb = SimEmbedder()
        import io as _io
        from PIL import Image
        import numpy as _np
        rgb = _np.asarray(Image.open(_io.BytesIO(frame(cats=[CAT]))))
        from robok.vision.types import crop
        c = crop(rgb, CAT, 0.05)
        for _ in range(n):
            self.gallery.add_sample(ident["id"], emb.embed(c))
        return ident["id"]


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def track_of(res, kind):
    return next((t for t in res["tracks"] if t["kind"] == kind), None)


# --- detectar e identificar ---------------------------------------------------------------------------------

def test_detecta_gato_y_persona_sin_identidades(rig):
    res = rig.step(frame(cats=[CAT], person=PERSON))
    assert sorted(t["kind"] for t in res["tracks"]) == ["cat", "person"]
    assert all(t["identity"] is None and t["candidate"] is None for t in res["tracks"])
    assert (res["w"], res["h"]) == (320, 240) and res["detect_ms"] >= 0
    json.dumps(res)                                                       # el resultado se envía tal cual por WebSocket


def test_solo_los_tipos_configurados(tmp_path):
    r = Rig(tmp_path, kinds=("cat",))
    assert [t["kind"] for t in r.step(frame(cats=[CAT], person=PERSON))["tracks"]] == ["cat"]


def test_reconoce_al_gato_ensenado_tras_confirmarlo_con_votos(rig):
    rig.enroll_cat("Pila")
    first = rig.step(frame(cats=[CAT]))
    assert track_of(first, "cat")["identity"] is None, "un solo voto no basta para decidir"
    res = rig.step(frame(cats=[CAT]), n=2)
    ident = track_of(res, "cat")["identity"]
    assert ident["name"] == "Pila" and ident["score"] > 0.9 and ident["follow"] is True


def test_un_gato_desconocido_no_recibe_el_nombre_de_pila(rig):
    rig.enroll_cat("Pila")
    res = rig.step(frame(gray=GRAYCAT), n=4)
    t = track_of(res, "cat")
    assert t["identity"] is None
    assert t["candidate"] is None or t["candidate"]["score"] < rig.cfg.cat_threshold


def test_dos_gatos_distintos_se_distinguen_entre_si(rig):
    rig.enroll_cat("Pila")                                                # naranja
    grey = rig.gallery.create("Nube", "cat")
    import io
    from PIL import Image
    from robok.vision.types import crop
    rgb = np.asarray(Image.open(io.BytesIO(frame(gray=GRAYCAT))))
    for _ in range(3):
        rig.gallery.add_sample(grey["id"], SimEmbedder().embed(crop(rgb, GRAYCAT, 0.05)))
    res = rig.step(frame(cats=[CAT_RIGHT], gray=GRAYCAT), n=4)
    names = {t["identity"]["name"] for t in res["tracks"] if t["identity"]}
    assert names == {"Pila", "Nube"}
    by_x = sorted(res["tracks"], key=lambda t: t["box"][0])
    assert [t["identity"]["name"] for t in by_x] == ["Nube", "Pila"], "cada nombre en su gato"


def test_sin_identidades_de_ese_tipo_no_se_gasta_en_huellas(rig, monkeypatch):
    calls = []
    monkeypatch.setattr(rig.vision._cat_embedder, "embed", lambda c: calls.append(1) or np.ones(4, np.float32))
    rig.step(frame(cats=[CAT]), n=5)
    assert calls == []


def test_reconoce_a_la_persona_por_su_cara_y_la_conserva_al_volverse(rig):
    pid = rig.gallery.create("Ana", "person")["id"]
    import io
    from PIL import Image
    from robok.vision.types import crop
    rgb = np.asarray(Image.open(io.BytesIO(frame(person=PERSON))))
    face = SimFaces().extract(crop(rgb, (PERSON[0], PERSON[1], PERSON[2], PERSON[1] + (PERSON[3] - PERSON[1]) * 0.6), 0.05))
    for _ in range(2):
        rig.gallery.add_sample(pid, face.embedding)
    res = rig.step(frame(person=PERSON), dt=0.7, n=3)
    assert track_of(res, "person")["identity"]["name"] == "Ana"
    res = rig.step(frame(person=PERSON, skin=False), dt=0.7, n=4)          # de espaldas: no se ve la cara
    assert track_of(res, "person")["identity"]["name"] == "Ana", "no pierde el nombre por no ver la cara un rato"


def test_como_mucho_2_huellas_por_ciclo(rig, monkeypatch):
    rig.enroll_cat("Pila")
    calls = []
    orig = rig.vision._cat_embedder.embed
    monkeypatch.setattr(rig.vision._cat_embedder, "embed", lambda c: calls.append(1) or orig(c))
    from tests.vision_helpers import frame as fr
    # tres "gatos" naranjas en el cuadro (el simulador los junta en una caja): forzamos 3 pistas con un detector propio
    class Three:
        desc = "3"
        def detect(self, rgb):
            from robok.vision.types import Detection
            return [Detection("cat", .9, (.05 + .3 * i, .4, .25 + .3 * i, .8)) for i in range(3)]
    rig.vision._detector = Three()
    rig.step(fr(cats=[CAT]))
    assert len(calls) == 2


# --- mirada -------------------------------------------------------------------------------------------------

def test_los_ojos_siguen_a_la_identidad_marcada_con_seguir(rig):
    rig.enroll_cat("Pila", follow=True)
    rig.step(frame(cats=[CAT_RIGHT]), n=3)
    x, y = rig.looks[-1]
    # gato a la derecha del cuadro (centro x=0.8): en espejo los ojos van a la izquierda de la pantalla
    assert x == pytest.approx(-(0.8 - 0.5) * 2 * 1.3, abs=0.02)
    assert y > 0, "el gato está en la parte baja del cuadro: los ojos miran un poco hacia abajo"
    assert rig.vision.status()["following"] == "Pila"


def test_sin_espejo_los_ojos_van_hacia_el_mismo_lado_del_cuadro(tmp_path):
    r = Rig(tmp_path, gaze_mirror=False)
    r.enroll_cat("Pila")
    r.step(frame(cats=[CAT_RIGHT]), n=3)
    assert r.looks[-1][0] > 0.5
    r.vision.configure(gaze_mirror=True)
    r.step(frame(cats=[CAT_LEFT]), n=3)
    assert r.looks[-1][0] > 0.5, "izquierda del cuadro + espejo = derecha de la pantalla"


def test_la_mirada_sigue_el_movimiento_del_gato(rig):
    rig.enroll_cat("Pila")
    rig.step(frame(cats=[CAT_LEFT]), n=4)
    left = rig.looks[-1][0]
    for _ in range(4):
        rig.step(frame(cats=[CAT_RIGHT]))
    assert left > 0.5 and rig.looks[-1][0] < -0.5, "los ojos cruzaron de un lado al otro"


def test_no_sigue_a_una_identidad_sin_seguir_ni_a_un_gato_desconocido(rig):
    rig.enroll_cat("Pila", follow=False)
    rig.step(frame(cats=[CAT_RIGHT]), n=4)
    assert rig.looks == [] and rig.vision.status()["following"] is None
    rig.step(frame(gray=GRAYCAT), n=4)
    assert rig.looks == []


def test_al_perder_al_gato_los_ojos_vuelven_al_centro_una_sola_vez(rig):
    rig.enroll_cat("Pila")
    rig.step(frame(cats=[CAT_RIGHT]), n=3)
    n = len(rig.looks)
    rig.step(frame(), dt=0.5, n=2)                                        # 1 s sin verlo: aún dentro de lost_s (1,5)
    assert rig.vision.status()["following"] == "Pila" and len(rig.looks) == n
    rig.step(frame(), dt=0.5, n=2)
    assert rig.looks[-1] == (0.0, 0.0) and rig.vision.status()["following"] is None
    total = len(rig.looks)
    rig.step(frame(), dt=0.5, n=4)
    assert len(rig.looks) == total, "no repite la orden de centrar"


class FixedDetector:
    desc = "fijo"

    def __init__(self, dets):
        self.dets = dets

    def detect(self, rgb):
        return list(self.dets)


def test_si_hay_dos_gatos_seguidos_mira_al_mas_grande(rig):
    from robok.vision.types import Detection
    rig.enroll_cat("Pila")
    big, small = (0.55, 0.30, 0.95, 0.85), (0.05, 0.60, 0.15, 0.75)
    rig.vision._detector = FixedDetector([Detection("cat", .9, big), Detection("cat", .9, small)])
    jpg = frame(cats=[big, small])
    for _ in range(4):
        res = rig.step(jpg)
    assert len([t for t in res["tracks"] if t["identity"]]) == 2, "los dos son Pila (mismo color)"
    assert rig.looks[-1][0] < -0.5, "el grande está a la derecha: mira hacia allá (espejo), no al chico de la izquierda"


def test_borrar_la_identidad_mientras_se_sigue_no_rompe(rig):
    iid = rig.enroll_cat("Pila")
    rig.step(frame(cats=[CAT_RIGHT]), n=3)
    rig.gallery.delete(iid)
    res = rig.step(frame(cats=[CAT_RIGHT]), dt=0.5, n=4)
    assert track_of(res, "cat")["identity"] is None, "sin identidad ya no hay nombre"
    assert rig.vision.status()["following"] is None


# --- enseñar ----------------------------------------------------------------------------------------------------

def teach_frames(rig, patches):
    for p in patches:
        rig.step(frame(cats=[CAT], patch=p), dt=0.8)


def test_ensenar_un_gato_toma_muestras_y_termina(rig):
    gid = rig.gallery.create("Pila", "cat", True)["id"]
    st = rig.vision.start_teaching(gid)
    assert st["state"] == "active" and st["target"] == 4 and st["collected"] == 0
    teach_frames(rig, [0.0, 0.2, 0.4, 0.6, 0.8])
    t = rig.vision.status()["teach"]
    assert t["state"] == "done" and t["collected"] == 4
    ident = rig.gallery.get(gid)
    assert len(ident["samples"]) == 4 and all(rig.gallery.thumb_path(gid, s["id"]) for s in ident["samples"])
    assert rig.events == ["teach_done"]
    # y ahora sí lo reconoce
    res = rig.step(frame(cats=[CAT_RIGHT], patch=0.3), n=3)
    assert track_of(res, "cat")["identity"]["name"] == "Pila"


def test_sin_gato_a_la_vista_espera_y_avisa(rig):
    gid = rig.gallery.create("Pila", "cat")["id"]
    rig.vision.start_teaching(gid)
    rig.step(frame(), dt=0.8, n=2)
    t = rig.vision.status()["teach"]
    assert t["collected"] == 0 and "no veo ningún gato" in t["hint"]


def test_con_dos_gatos_a_la_vista_no_toma_muestras(rig):
    gid = rig.gallery.create("Pila", "cat")["id"]
    rig.vision.start_teaching(gid)
    rig.step(frame(cats=[CAT_RIGHT], gray=GRAYCAT), dt=0.8, n=3)
    t = rig.vision.status()["teach"]
    assert t["collected"] == 0 and "2 gatos" in t["hint"] and rig.gallery.get(gid)["samples"] == []
    rig.step(frame(cats=[CAT_RIGHT], patch=0.1), dt=0.8, n=1)              # se va el gris: ahora sí
    assert rig.vision.status()["teach"]["collected"] == 1


def test_muestras_casi_iguales_se_omiten(rig):
    gid = rig.gallery.create("Pila", "cat")["id"]
    rig.vision.start_teaching(gid)
    rig.step(frame(cats=[CAT]), dt=0.8, n=5)                               # el gato no se mueve: siempre la misma huella
    t = rig.vision.status()["teach"]
    assert t["collected"] == 1 and "muévete" in t["hint"]


def test_ensenar_solo_toma_muestras_cada_cierto_tiempo(rig):
    gid = rig.gallery.create("Pila", "cat")["id"]
    rig.vision.start_teaching(gid)
    for p in (0.0, 0.3, 0.6):
        rig.step(frame(cats=[CAT], patch=p), dt=0.1)                       # 3 cuadros en 0,3 s
    assert rig.vision.status()["teach"]["collected"] == 1


def test_ensenanza_con_tiempo_agotado_guarda_lo_tomado(tmp_path):
    r = Rig(tmp_path, teach_timeout_s=5.0)
    gid = r.gallery.create("Pila", "cat")["id"]
    r.vision.start_teaching(gid)
    r.step(frame(cats=[CAT], patch=0.0), dt=0.8)
    r.step(frame(cats=[CAT], patch=0.5), dt=6.0)
    t = r.vision.status()["teach"]
    assert t["state"] == "timeout" and t["collected"] == 1 and len(r.gallery.get(gid)["samples"]) == 1
    assert r.events == ["teach_timeout"]


def test_cancelar_y_no_empezar_dos_a_la_vez(rig):
    gid = rig.gallery.create("Pila", "cat")["id"]
    rig.vision.start_teaching(gid)
    with pytest.raises(VisionError, match="en curso"):
        rig.vision.start_teaching(gid)
    rig.vision.cancel_teaching()
    assert rig.vision.status()["teach"]["state"] == "cancelled"
    rig.step(frame(cats=[CAT]), dt=0.8, n=2)
    assert rig.gallery.get(gid)["samples"] == [], "cancelada: ya no toma muestras"
    rig.vision.start_teaching(gid)                                         # y se puede volver a empezar


def test_el_resultado_de_la_ensenanza_se_conserva_un_rato_y_luego_desaparece(rig):
    gid = rig.gallery.create("Pila", "cat")["id"]
    rig.vision.start_teaching(gid, samples=3)
    teach_frames(rig, [0.0, 0.3, 0.6])
    assert rig.vision.status()["teach"]["state"] == "done"
    rig.step(frame(), dt=vs.TEACH_RESULT_KEEP_S + 1)
    assert rig.vision.status()["teach"] is None


def test_ensenar_errores(rig, tmp_path):
    with pytest.raises(GalleryError):
        rig.vision.start_teaching("g-fantasma")
    gid = rig.gallery.create("Pila", "cat")["id"]
    with pytest.raises(ValueError):
        rig.vision.start_teaching(gid, samples=1)
    rig.vision.configure(enabled=False)
    with pytest.raises(VisionError, match="apagada"):
        rig.vision.start_teaching(gid)


def test_ensenar_a_una_persona_pide_ver_la_cara(rig):
    pid = rig.gallery.create("Ana", "person")["id"]
    rig.vision.start_teaching(pid, samples=3)
    rig.step(frame(person=PERSON, skin=False), dt=0.8, n=2)
    t = rig.vision.status()["teach"]
    assert t["collected"] == 0 and "cara" in t["hint"]
    for _ in range(3):
        rig.step(frame(person=PERSON), dt=0.8)
        rig.vision.status()
    # la huella de la cara simulada es siempre igual: solo entra la primera y las demás se omiten como duplicadas
    assert rig.vision.status()["teach"]["collected"] == 1


def test_ensenar_persona_rechaza_caras_diminutas(rig, monkeypatch):
    from robok.vision.faces import Face
    pid = rig.gallery.create("Ana", "person")["id"]
    rig.vision.start_teaching(pid, samples=3)
    real = rig.vision._faces.extract
    monkeypatch.setattr(rig.vision._faces, "extract",
                        lambda c: (lambda f: Face(f.embedding, f.box, f.score, 12))(real(c)))
    rig.step(frame(person=PERSON), dt=0.8, n=2)
    t = rig.vision.status()["teach"]
    assert t["collected"] == 0 and "acércate" in t["hint"]


# --- ajustes y disponibilidad --------------------------------------------------------------------------------------

def test_los_ajustes_se_recuerdan_entre_arranques(tmp_path):
    r = Rig(tmp_path)
    r.vision.configure(enabled=False, gaze_mirror=False)
    r2 = Rig(tmp_path)                                                       # "reinicio": mismo directorio de ajustes
    st = r2.vision.status()
    assert st["enabled"] is False and st["gaze_mirror"] is False
    assert json.loads((tmp_path / "settings.json").read_text()) == {"enabled": False, "gaze_mirror": False}


def test_ajustes_corruptos_se_ignoran(tmp_path):
    (tmp_path / "settings.json").write_text("{{{")
    assert Rig(tmp_path).vision.status()["enabled"] is True


def test_sin_modelos_no_esta_disponible_y_no_se_puede_encender(tmp_path):
    v = Vision(VisionCfg(), StubCamera(), Gallery(tmp_path / "g"), None, None, None, lambda x, y: None,
               unavailable="faltan los modelos: ejecuta scripts/get_models.py")
    st = v.status()
    assert st["available"] is False and st["enabled"] is False and "get_models" in st["reason"]
    with pytest.raises(VisionError, match="no está disponible"):
        v.configure(enabled=True)
    with pytest.raises(VisionError):
        v.start_teaching("g-x")
    v.start(); v.close()                                                     # el hilo no hace nada y cierra limpio


def test_sin_camara_no_esta_disponible(tmp_path):
    v = Vision(VisionCfg(), None, Gallery(tmp_path / "g"), SimDetector(), None, None, lambda x, y: None)
    assert v.status()["reason"] == "no hay cámara"


def test_menos_analisis_si_la_pi_se_calienta(tmp_path):
    temp = {"v": 50.0}
    v = Vision(VisionCfg(fps=4), StubCamera(), Gallery(tmp_path / "g"), SimDetector(), None, None, lambda x, y: None,
               temp_fn=lambda: temp["v"])
    assert v._fps_now() == 4
    temp["v"] = 77; assert v._fps_now() == 2
    temp["v"] = 85; assert v._fps_now() == 1
    temp["v"] = None; assert v._fps_now() == 4


# --- el hilo --------------------------------------------------------------------------------------------------------------

def wait_for(cond, timeout=4.0, step=0.01):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


@pytest.fixture
def running(tmp_path):
    r = Rig(tmp_path, fps=15, real_clock=True)
    r.vision.start()
    yield r
    r.vision.close()


def test_el_hilo_toma_la_camara_analiza_y_publica(running):
    got = []
    running.vision.subscribe(got.append)
    running.cam.put(frame(cats=[CAT], person=PERSON))
    assert wait_for(lambda: len(got) >= 1)
    assert running.cam.acquired == 1 and sorted(t["kind"] for t in got[0]["tracks"]) == ["cat", "person"]
    n = len(got)
    time.sleep(0.3)
    assert len(got) == n, "el mismo fotograma no se analiza dos veces"
    running.cam.put(frame(cats=[CAT_RIGHT]))
    assert wait_for(lambda: len(got) > n)
    assert got[-1]["seq"] > got[0]["seq"]


def test_un_fotograma_viejo_se_ignora(running):
    got = []
    running.vision.subscribe(got.append)
    running.cam.put(frame(cats=[CAT]))
    running.cam._at -= 10                                                     # llegó hace 10 s
    time.sleep(0.4)
    assert got == []


def test_apagar_suelta_la_camara_y_devuelve_los_ojos_al_centro(running):
    running.enroll_cat("Pila")
    for _ in range(4):
        running.cam.put(frame(cats=[CAT_RIGHT]))
        time.sleep(0.12)
    assert wait_for(lambda: running.vision.status()["following"] == "Pila")
    running.vision.configure(enabled=False)
    assert wait_for(lambda: running.cam.released == 1)
    assert running.looks[-1] == (0.0, 0.0) and running.vision.status()["following"] is None
    running.vision.configure(enabled=True)
    assert wait_for(lambda: running.cam.acquired == 2), "al reencender vuelve a pedir la cámara"


def test_si_la_camara_se_queda_muda_los_ojos_vuelven_al_centro(tmp_path):
    r = Rig(tmp_path, fps=15, lost_s=0.3, real_clock=True)
    r.enroll_cat("Pila")
    r.vision.start()
    try:
        for _ in range(4):
            r.cam.put(frame(cats=[CAT_RIGHT]))
            time.sleep(0.12)
        assert wait_for(lambda: r.vision.status()["following"] == "Pila")
        assert wait_for(lambda: r.vision.status()["following"] is None, timeout=4), "sin imágenes también se pierde al gato"
        assert r.looks[-1] == (0.0, 0.0)
    finally:
        r.vision.close()


def test_un_error_del_detector_no_mata_el_hilo_y_se_recupera(running, monkeypatch):
    calls = {"n": 0}
    real = running.vision._detector.detect

    def flaky(rgb):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("fallo del modelo")
        return real(rgb)
    monkeypatch.setattr(running.vision._detector, "detect", flaky)
    got = []
    running.vision.subscribe(got.append)
    for _ in range(60):
        running.cam.put(frame(cats=[CAT]))
        time.sleep(0.06)
        if got:
            break
    assert got and calls["n"] >= 3
    assert wait_for(lambda: running.vision.status()["error"] is None)
    assert running.vision.status()["running"]


def test_un_suscriptor_roto_se_da_de_baja(running):
    good = []
    running.vision.subscribe(lambda r: 1 / 0)
    running.vision.subscribe(good.append)
    running.cam.put(frame(cats=[CAT]))
    assert wait_for(lambda: len(good) >= 1)
    assert len(running.vision._subs) == 1


def test_close_es_idempotente_y_suelta_todo(tmp_path):
    r = Rig(tmp_path, fps=15, real_clock=True)
    r.vision.start()
    r.cam.put(frame(cats=[CAT]))
    assert wait_for(lambda: r.cam.acquired == 1)
    r.vision.close(); r.vision.close()
    assert r.cam.released == 1
