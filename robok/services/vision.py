"""Visión de Wally: detecta personas, gatos y perros, reconoce a quién conoce y mueve los ojos tras el gato elegido.

Un hilo analiza `fps` cuadros por segundo de la cámara (que mantiene encendida mientras la visión está activa):
    JPEG -> detector -> seguimiento -> identidad (huella + galería) -> enseñanza / mirada -> resultado

- **Identidad**: cada objeto seguido acumula "votos" de lo que la galería opina; se decide por votos (no por una
  sola foto) y las personas conservan su identidad unos segundos aunque se vuelvan de espaldas.
- **Enseñar**: una sesión toma muestras de UN solo sujeto del tipo pedido (si hay dos gatos a la vista no sabe cuál
  es cuál, así que espera) y las guarda en la galería.
- **Mirada**: si el objeto reconocido es de una identidad con `follow`, los ojos de la cara siguen su posición;
  al perderlo `lost_s` segundos, vuelven al centro.

`process_frame()` hace un ciclo completo sin hilos ni cámara: es lo que usan el bucle y las pruebas.
"""
from __future__ import annotations

import io
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image

from robok.config import VisionCfg
from robok.hal.camera import Camera
from robok.vision.detector import Detector
from robok.vision.embedder import Embedder
from robok.vision.faces import FaceRecognizer
from robok.vision.gallery import Gallery, GalleryError
from robok.vision.tracker import Track, Tracker
from robok.vision.types import IDENTIFIABLE, KIND_ES, Box, crop

log = logging.getLogger(__name__)

MAX_FRAME_AGE_S = 1.5        # un fotograma más viejo que esto no se analiza (evita reaccionar a algo del pasado)
IDENTIFY_PER_CYCLE = 2       # como mucho 2 huellas por ciclo: acota la latencia
RECHECK_STABLE_S = 2.0       # una identidad ya confiable se vuelve a comprobar cada tanto
RECHECK_NEW_S = 0.2          # mientras se decide, en casi cada ciclo
RECHECK_FACE_S = 0.6         # buscar una cara cuesta ~200 ms: no en cada ciclo
TEACH_INTERVAL_S = 0.7
TEACH_MIN_FACE_PX = 40
TEACH_MIN_FACE_SCORE = 0.8
TEACH_DUP_COS = 0.985
TEACH_RESULT_KEEP_S = 20.0
THUMB_PX = 96


class VisionError(RuntimeError):
    """La visión no puede hacer lo pedido (apagada, sin modelos...)."""


def _thumb_jpeg(rgb_crop: np.ndarray) -> bytes:
    img = Image.fromarray(rgb_crop)
    img.thumbnail((THUMB_PX, THUMB_PX))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return buf.getvalue()


def _decode(jpeg: bytes) -> np.ndarray:
    with Image.open(io.BytesIO(jpeg)) as im:
        return np.asarray(im.convert("RGB"))


class Vision:
    def __init__(self, cfg: VisionCfg, camera: Camera | None, gallery: Gallery, detector: Detector | None,
                 cat_embedder: Embedder | None, faces: FaceRecognizer | None,
                 look: Callable[[float, float], None], *, unavailable: str | None = None,
                 settings_path: str | Path | None = None, clock: Callable[[], float] = time.monotonic,
                 temp_fn: Callable[[], float | None] = lambda: None,
                 on_event: Callable[[str], None] | None = None, desc: str = ""):
        self.cfg, self.camera, self.gallery = cfg, camera, gallery
        self._detector, self._cat_embedder, self._faces = detector, cat_embedder, faces
        self._look, self._clock, self._temp, self._on_event = look, clock, temp_fn, on_event
        self.unavailable = unavailable if unavailable else (
            None if camera is not None and detector is not None else "no hay cámara" if camera is None else "faltan modelos")
        self.desc = desc
        self._settings_path = Path(settings_path) if settings_path else None
        self._lock = threading.RLock()
        self._enabled = cfg.enabled and self.unavailable is None
        self._mirror = cfg.gaze_mirror
        self._load_settings()
        self._tracker = Tracker()
        self._latest: dict | None = None
        self._subs: list[Callable[[dict], None]] = []
        self._seq = 0
        self._teach: dict | None = None
        self._following: str | None = None        # nombre de a quién siguen los ojos ahora
        self._last_follow_seen = 0.0
        self._gaze = (0.0, 0.0)
        self._detect_ms = 0.0
        self._fps = 0.0
        self._times: list[float] = []
        self._error: str | None = None
        self._errors = 0
        self._acquired = False
        self._last_frame_seq = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # --- ajustes que el usuario cambia desde la web (se recuerdan entre arranques) --------------------------
    def _load_settings(self) -> None:
        if self._settings_path is None or not self._settings_path.exists():
            return
        try:
            data = json.loads(self._settings_path.read_text())
            if self.unavailable is None and isinstance(data.get("enabled"), bool):
                self._enabled = data["enabled"]
            if isinstance(data.get("gaze_mirror"), bool):
                self._mirror = data["gaze_mirror"]
        except Exception as e:
            log.warning("no se pudieron leer los ajustes de visión (%s); se usan los de config.toml", e)

    def _save_settings(self) -> None:
        if self._settings_path is None:
            return
        try:
            self._settings_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._settings_path.with_name(self._settings_path.name + ".tmp")
            tmp.write_text(json.dumps({"enabled": self._enabled, "gaze_mirror": self._mirror}))
            os.replace(tmp, self._settings_path)
        except OSError as e:
            log.warning("no se pudieron guardar los ajustes de visión: %s", e)

    def configure(self, enabled: bool | None = None, gaze_mirror: bool | None = None) -> None:
        with self._lock:
            if enabled is not None:
                if enabled and self.unavailable:
                    raise VisionError(f"la visión no está disponible: {self.unavailable}")
                self._enabled = bool(enabled)
            if gaze_mirror is not None:
                self._mirror = bool(gaze_mirror)
            self._save_settings()
        if enabled is False:
            self._end_session_effects()

    @property
    def enabled(self) -> bool:
        return self._enabled

    # --- ciclo de análisis ---------------------------------------------------------------------------------
    def process_frame(self, jpeg: bytes, now: float | None = None) -> dict:
        now = self._clock() if now is None else now
        rgb = _decode(jpeg)
        t0 = time.perf_counter()
        dets = [d for d in self._detector.detect(rgb) if d.kind in self.cfg.kinds]      # type: ignore[union-attr]
        self._detect_ms = 0.7 * self._detect_ms + 0.3 * (time.perf_counter() - t0) * 1000 if self._detect_ms else \
            (time.perf_counter() - t0) * 1000
        tracks = self._tracker.update(dets, now)
        self._identify(tracks, rgb, now)
        self._teach_step(tracks, rgb, now)
        self._follow(tracks, now)
        return self._publish(tracks, rgb.shape[1], rgb.shape[0], now)

    def _person_crop(self, rgb: np.ndarray, box: Box) -> np.ndarray | None:
        x0, y0, x1, y1 = box
        return crop(rgb, (x0, y0, x1, y0 + (y1 - y0) * 0.6), pad=0.05)      # cabeza y torso: ahí está la cara

    def _identify(self, tracks: list[Track], rgb: np.ndarray, now: float) -> None:
        todo = []
        for t in tracks:
            if t.kind not in IDENTIFIABLE or self.gallery.count(t.kind) == 0:
                continue
            if t.kind == "person" and (self._faces is None or t.box[3] - t.box[1] < 0.25):
                continue
            if t.kind == "cat" and self._cat_embedder is None:
                continue
            stable = len(t.votes) >= 3 and t.identity_id is not None
            gap = RECHECK_STABLE_S if stable else (RECHECK_FACE_S if t.kind == "person" else RECHECK_NEW_S)
            if now - t.last_vote_at >= gap:
                todo.append((t.last_vote_at, t))
        for _, t in sorted(todo, key=lambda x: x[0])[:IDENTIFY_PER_CYCLE]:
            thr = self.cfg.cat_threshold if t.kind == "cat" else self.cfg.person_threshold
            try:
                if t.kind == "cat":
                    c = crop(rgb, t.box, pad=0.05)
                    emb = None if c is None else self._cat_embedder.embed(c)             # type: ignore[union-attr]
                else:
                    c = self._person_crop(rgb, t.box)
                    face = None if c is None else self._faces.extract(c)                # type: ignore[union-attr]
                    emb = None if face is None else face.embedding
                    if face is not None:
                        t.face_seen_at = now
            except Exception:
                log.exception("no se pudo sacar la huella del objeto %d", t.id)
                continue
            t.last_vote_at = now
            if emb is None:
                if t.kind == "cat":
                    t.add_vote(None, 0.0, now)
                continue                                    # sin cara no hay voto: la persona conserva su identidad
            m = self.gallery.match(t.kind, emb, thr, self.cfg.margin)
            t.cand_id, t.cand_score = m.best_id, m.score
            t.add_vote(m.identity_id, m.score, now)

    # --- enseñanza -------------------------------------------------------------------------------------------
    def start_teaching(self, identity_id: str, samples: int | None = None) -> dict:
        if self.unavailable:
            raise VisionError(f"la visión no está disponible: {self.unavailable}")
        if not self._enabled:
            raise VisionError("la visión está apagada: enciéndela para enseñar")
        ident = self.gallery.get(identity_id)
        if ident is None:
            raise GalleryError(f"no existe la identidad {identity_id!r}")
        if ident["kind"] == "person" and self._faces is None or ident["kind"] == "cat" and self._cat_embedder is None:
            raise VisionError("falta el modelo para este tipo de identidad")
        n = samples or self.cfg.teach_samples
        if not 3 <= n <= 40:
            raise ValueError("samples debe estar entre 3 y 40")
        now = self._clock()
        with self._lock:
            if self._teach and self._teach["state"] == "active":
                raise VisionError("ya hay una enseñanza en curso: cancélala primero")
            self._teach = {"identity_id": identity_id, "name": ident["name"], "kind": ident["kind"], "target": n,
                           "collected": 0, "state": "active", "started": now, "deadline": now + self.cfg.teach_timeout_s,
                           "last_at": 0.0, "embs": [], "hint": f"busco {KIND_ES[ident['kind']]}…", "ended": 0.0}
            return self._teach_public()

    def cancel_teaching(self) -> None:
        with self._lock:
            if self._teach and self._teach["state"] == "active":
                self._finish_teach("cancelled", "enseñanza cancelada", self._clock())

    def _finish_teach(self, state: str, hint: str, now: float) -> None:
        self._teach.update(state=state, hint=hint, ended=now)                        # type: ignore[union-attr]

    def _teach_public(self) -> dict | None:
        t = self._teach
        if t is None:
            return None
        return {"identity_id": t["identity_id"], "name": t["name"], "kind": t["kind"], "state": t["state"],
                "collected": t["collected"], "target": t["target"], "hint": t["hint"]}

    def _teach_step(self, tracks: list[Track], rgb: np.ndarray, now: float) -> None:
        with self._lock:
            t = self._teach
            if t is None:
                return
            if t["state"] != "active":
                if now - t["ended"] > TEACH_RESULT_KEEP_S:
                    self._teach = None
                return
            if now > t["deadline"]:
                self._finish_teach("timeout", f"se acabó el tiempo ({t['collected']} muestras guardadas)", now)
                self._emit("teach_timeout")
                return
            kind, word = t["kind"], KIND_ES[t["kind"]]
            subjects = [x for x in tracks if x.kind == kind]
            if not subjects:
                t["hint"] = f"no veo ningún {word}" if kind == "cat" else "no veo a nadie"
                return
            if len(subjects) > 1:
                t["hint"] = f"veo {len(subjects)} {word}s a la vez: deja solo a {t['name']} frente a la cámara"
                return
            if now - t["last_at"] < TEACH_INTERVAL_S:
                return
            subject = subjects[0]
        # fuera del candado: sacar la huella cuesta decenas de ms
        try:
            if kind == "cat":
                c = crop(rgb, subject.box, pad=0.05)
                emb, thumb = (None, None) if c is None else (self._cat_embedder.embed(c), _thumb_jpeg(c))  # type: ignore[union-attr]
                if emb is None:
                    return
                bad = None
            else:
                pc = self._person_crop(rgb, subject.box)
                face = None if pc is None else self._faces.extract(pc)                  # type: ignore[union-attr]
                if face is None:
                    bad, emb, thumb = "no veo la cara: mírame de frente", None, None
                elif face.px < TEACH_MIN_FACE_PX or face.score < TEACH_MIN_FACE_SCORE:
                    bad, emb, thumb = "acércate un poco y mírame de frente", None, None
                else:
                    fx0, fy0, fx1, fy1 = face.box
                    fc = crop(pc, (fx0, fy0, fx1, fy1), pad=0.3)
                    emb, thumb, bad = face.embedding, _thumb_jpeg(fc if fc is not None else pc), None
        except Exception:
            log.exception("falló la toma de muestra")
            return
        with self._lock:
            t = self._teach
            if t is None or t["state"] != "active":
                return
            if emb is None:
                t["hint"] = bad or t["hint"]
                return
            t["last_at"] = now
            if any(float(np.dot(emb, e)) > TEACH_DUP_COS for e in t["embs"][-3:]):
                t["hint"] = "casi igual a la anterior: muévete o cambia de ángulo"
                return
            try:
                self.gallery.add_sample(t["identity_id"], emb, thumb)
            except GalleryError as e:
                self._finish_teach("error", str(e), now)
                return
            t["embs"].append(emb)
            t["collected"] += 1
            t["hint"] = "bien, sigue moviéndote un poco" if kind == "cat" else "bien, gira un poco la cabeza"
            if t["collected"] >= t["target"]:
                self._finish_teach("done", f"listo: {t['collected']} muestras de {t['name']}", now)
                self._emit("teach_done")

    def _emit(self, kind: str) -> None:
        if self._on_event:
            try:
                self._on_event(kind)
            except Exception:
                log.exception("error en el manejador del evento %s", kind)

    # --- mirada ------------------------------------------------------------------------------------------------
    def gaze_for(self, box: Box) -> tuple[float, float]:
        """Posición de la mirada (-1..1) para un objeto en `box`; los ojos miran hacia él."""
        cx, cy = (box[0] + box[2]) / 2, box[1] + (box[3] - box[1]) * 0.4       # un poco por encima del centro: la cabeza
        g = self.cfg.follow_gain
        x = max(-1.0, min(1.0, (cx - 0.5) * 2 * g))
        y = max(-1.0, min(1.0, (cy - 0.5) * 2 * g))
        return (-x if self._mirror else x), y

    def _follow(self, tracks: list[Track], now: float) -> None:
        best: tuple[float, Track, str] | None = None
        for t in tracks:
            if t.identity_id is None:
                continue
            ident = self.gallery.get(t.identity_id)
            if ident is None or not ident["follow"]:
                continue
            rank = t.score * max(0.05, (t.box[2] - t.box[0]) * (t.box[3] - t.box[1]))
            if best is None or rank > best[0]:
                best = (rank, t, ident["name"])
        if best is not None:
            _, t, name = best
            gx, gy = self.gaze_for(t.box)
            self._gaze = (0.5 * self._gaze[0] + 0.5 * gx, 0.5 * self._gaze[1] + 0.5 * gy) if self._following else (gx, gy)
            self._look(*self._gaze)
            self._following, self._last_follow_seen = name, now
        elif self._following is not None and now - self._last_follow_seen > self.cfg.lost_s:
            self._end_session_effects()

    def _end_session_effects(self) -> None:
        """Los ojos vuelven al centro."""
        if self._following is not None:
            self._following = None
            self._gaze = (0.0, 0.0)
            try:
                self._look(0.0, 0.0)
            except Exception:
                log.exception("no se pudo recentrar la mirada")

    # --- resultado ------------------------------------------------------------------------------------------------
    def _publish(self, tracks: list[Track], w: int, h: int, now: float) -> dict:
        names = {i["id"]: i for i in self.gallery.list()}
        out = []
        for t in sorted(tracks, key=lambda x: x.id):
            ident = names.get(t.identity_id) if t.identity_id else None
            cand = names.get(t.cand_id) if t.cand_id else None
            out.append({
                "id": t.id, "kind": t.kind, "kind_es": KIND_ES[t.kind], "score": round(t.score, 2),
                "box": [round(v, 3) for v in t.box],
                "identity": None if ident is None else {"id": ident["id"], "name": ident["name"],
                                                       "score": round(t.identity_score, 2), "follow": ident["follow"]},
                "candidate": None if cand is None or ident is not None else {"name": cand["name"], "score": round(t.cand_score, 2)},
            })
        self._times = [x for x in self._times if now - x <= 3.0] + [now]
        self._fps = (len(self._times) - 1) / (self._times[-1] - self._times[0]) if len(self._times) > 1 else 0.0
        with self._lock:
            self._seq += 1
            result = {"type": "vision", "seq": self._seq, "w": w, "h": h, "tracks": out,
                      "following": self._following, "teach": self._teach_public(),
                      "detect_ms": round(self._detect_ms), "fps": round(self._fps, 1)}
            self._latest = result
            subs = list(self._subs)
        for cb in subs:
            try:
                cb(result)
            except Exception:
                log.exception("suscriptor de visión falló; se le da de baja")
                self.unsubscribe(cb)
        return result

    def subscribe(self, cb: Callable[[dict], None]) -> Callable[[], None]:
        with self._lock:
            self._subs.append(cb)
        return lambda: self.unsubscribe(cb)

    def unsubscribe(self, cb: Callable[[dict], None]) -> None:
        with self._lock:
            if cb in self._subs:
                self._subs.remove(cb)

    def latest(self) -> dict | None:
        return self._latest

    def status(self) -> dict:
        with self._lock:
            return {"available": self.unavailable is None, "enabled": self._enabled and self.unavailable is None,
                    "reason": self.unavailable, "desc": self.desc, "running": bool(self._thread and self._thread.is_alive()),
                    "fps": round(self._fps, 1) if self._enabled else 0.0, "detect_ms": round(self._detect_ms),
                    "error": self._error, "following": self._following, "gaze_mirror": self._mirror,
                    "teach": self._teach_public(), "identities": self.gallery.count()}

    # --- hilo --------------------------------------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="vision", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        self._release()
        self._end_session_effects()

    def _release(self) -> None:
        if self._acquired and self.camera is not None:
            self._acquired = False
            try:
                self.camera.release()
            except Exception:
                log.exception("no se pudo soltar la cámara")

    def _fps_now(self) -> float:
        temp = self._temp()
        scale = 1.0 if temp is None or temp < 75 else 0.5 if temp < 80 else 0.25    # PLAN: bajar el ritmo si se calienta
        return self.cfg.fps * scale

    def _run(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                if not self._enabled:
                    self._release()
                    self._end_session_effects()
                    self._stop.wait(0.25)
                    continue
                if not self._acquired:
                    self.camera.acquire()                                             # type: ignore[union-attr]
                    self._acquired = True
                frame, age = self.camera.latest(), self.camera.latest_age()           # type: ignore[union-attr]
                if frame is None or age is None or age > MAX_FRAME_AGE_S or frame[0] == self._last_frame_seq:
                    self._follow([], self._clock())                                   # sin imágenes también se pierde al gato
                    self._stop.wait(0.03)
                    continue
                self._last_frame_seq = frame[0]
                self.process_frame(frame[1])
                self._errors, self._error = 0, None
            except Exception as e:
                self._errors += 1
                self._error = f"{type(e).__name__}: {e}"
                log.exception("fallo en la visión (%d seguidos)", self._errors)
                self._stop.wait(min(5.0, 0.5 * self._errors))
            self._stop.wait(max(0.0, 1.0 / self._fps_now() - (time.monotonic() - t0)))
