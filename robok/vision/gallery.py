"""Galería de identidades: a quién conoce Wally (personas y gatos) y con qué muestras.

Disco (`data_dir`, fuera de git):
    gallery.json           identidades: id, nombre, tipo, follow y sus muestras
    samples/<id>.npy       la huella de cada muestra
    samples/<id>.jpg       una miniatura para ver y borrar muestras malas

Una identidad se reconoce comparando la huella nueva con todas sus muestras: su puntuación es el promedio de
las 3 similitudes más altas (coseno). Se acepta si supera el umbral y, cuando hay varias identidades del mismo
tipo, le saca `margin` a la segunda: sin ese margen, dos gatos parecidos se confundirían.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import secrets
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from robok.vision.types import IDENTIFIABLE

log = logging.getLogger(__name__)

MAX_IDENTITIES = 30
MAX_SAMPLES = 60
TOP_K = 3
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
SAMPLE_RE = re.compile(r"^[0-9a-f]{12}$")


class GalleryError(ValueError):
    """Petición no válida sobre la galería (nombre repetido, id inexistente, límite...)."""


@dataclass(frozen=True)
class Match:
    accepted: bool
    identity_id: str | None      # solo si se aceptó
    best_id: str | None          # el mejor candidato, se acepte o no
    score: float                 # puntuación del mejor candidato (0 si no hay identidades)
    second: float                # puntuación de la siguiente (0 si no hay)


def clean_name(name: str) -> str:
    if not isinstance(name, str):
        raise GalleryError("el nombre debe ser texto")
    name = " ".join(name.split())
    if not 1 <= len(name) <= 32 or any(unicodedata.category(c).startswith("C") for c in name):
        raise GalleryError("el nombre debe tener entre 1 y 32 caracteres, sin caracteres de control")
    return name


def slugify(name: str) -> str:
    ascii_ = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_.lower()).strip("-")[:32] or "id"


class Gallery:
    def __init__(self, data_dir: str | Path):
        self.dir = Path(data_dir)
        self._lock = threading.RLock()
        self._ids: dict[str, dict] = {}                  # id -> {"id","name","kind","follow","created","samples":[{"id","t"}]}
        self._emb: dict[str, np.ndarray] = {}            # id de muestra -> huella
        self._load()

    # --- persistencia -----------------------------------------------------------------------------
    @property
    def _json(self) -> Path:
        return self.dir / "gallery.json"

    def _load(self) -> None:
        if not self._json.exists():
            return
        try:
            data = json.loads(self._json.read_text())
            items = data["identities"]
            assert isinstance(items, list)
        except Exception as e:
            aside = self._json.with_name(f"gallery.json.corrupto-{int(time.time())}")
            log.error("gallery.json ilegible (%s); se aparta como %s y se empieza vacía", e, aside.name)
            os.replace(self._json, aside)
            return
        for it in items:
            try:
                ident = {"id": str(it["id"]), "name": clean_name(it["name"]), "kind": it["kind"],
                         "follow": bool(it.get("follow", False)), "created": float(it.get("created", 0)), "samples": []}
                if not ID_RE.match(ident["id"]) or ident["kind"] not in IDENTIFIABLE:
                    raise GalleryError("id o tipo no válidos")
                for s in it.get("samples", []):
                    sid = str(s["id"])
                    if not SAMPLE_RE.match(sid):
                        continue
                    try:
                        emb = np.load(self._sample_path(sid, "npy"), allow_pickle=False).astype(np.float32).ravel()
                        if not np.all(np.isfinite(emb)):
                            raise ValueError("huella con valores no finitos")
                    except Exception as e:
                        log.warning("muestra %s de %s descartada: %s", sid, ident["name"], e)
                        continue
                    self._emb[sid] = emb
                    ident["samples"].append({"id": sid, "t": float(s.get("t", 0))})
                self._ids[ident["id"]] = ident
            except Exception as e:
                log.warning("identidad descartada al cargar (%s): %r", e, it)

    def _sample_path(self, sid: str, ext: str) -> Path:
        return self.dir / "samples" / f"{sid}.{ext}"

    @staticmethod
    def _write_atomic(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def _save(self) -> None:
        payload = {"version": 1, "identities": list(self._ids.values())}
        self._write_atomic(self._json, json.dumps(payload, ensure_ascii=False, indent=1).encode())

    # --- consulta ------------------------------------------------------------------------------------
    def list(self) -> list[dict]:
        with self._lock:
            return [{**i, "samples": [dict(s) for s in i["samples"]]} for i in self._ids.values()]

    def get(self, identity_id: str) -> dict | None:
        with self._lock:
            i = self._ids.get(identity_id)
            return None if i is None else {**i, "samples": [dict(s) for s in i["samples"]]}

    def count(self, kind: str | None = None) -> int:
        with self._lock:
            return sum(1 for i in self._ids.values() if kind in (None, i["kind"]))

    def thumb_path(self, identity_id: str, sample_id: str) -> Path | None:
        if not SAMPLE_RE.match(sample_id):
            return None
        with self._lock:
            i = self._ids.get(identity_id)
            if i is None or not any(s["id"] == sample_id for s in i["samples"]):
                return None
        p = self._sample_path(sample_id, "jpg")
        return p if p.exists() else None

    # --- cambios ---------------------------------------------------------------------------------------
    def create(self, name: str, kind: str, follow: bool = False) -> dict:
        name = clean_name(name)
        if kind not in IDENTIFIABLE:
            raise GalleryError(f"tipo no válido: {kind!r} (válidos: {list(IDENTIFIABLE)})")
        with self._lock:
            if len(self._ids) >= MAX_IDENTITIES:
                raise GalleryError(f"límite de {MAX_IDENTITIES} identidades")
            if any(i["kind"] == kind and i["name"].casefold() == name.casefold() for i in self._ids.values()):
                raise GalleryError(f"ya existe {'un gato' if kind == 'cat' else 'una persona'} llamado {name!r}")
            base = f"{'g' if kind == 'cat' else 'p'}-{slugify(name)}"
            ident_id, n = base, 2
            while ident_id in self._ids:
                ident_id, n = f"{base}-{n}", n + 1
            self._ids[ident_id] = {"id": ident_id, "name": name, "kind": kind, "follow": bool(follow),
                                   "created": time.time(), "samples": []}
            self._save()
            return self.get(ident_id)

    def _need(self, identity_id: str) -> dict:
        i = self._ids.get(identity_id)
        if i is None:
            raise GalleryError(f"no existe la identidad {identity_id!r}")
        return i

    def update(self, identity_id: str, name: str | None = None, follow: bool | None = None) -> dict:
        with self._lock:
            i = self._need(identity_id)
            if name is not None:
                name = clean_name(name)
                if any(o is not i and o["kind"] == i["kind"] and o["name"].casefold() == name.casefold()
                       for o in self._ids.values()):
                    raise GalleryError(f"ya existe otro con el nombre {name!r}")
                i["name"] = name
            if follow is not None:
                i["follow"] = bool(follow)
            self._save()
            return self.get(identity_id)

    def delete(self, identity_id: str) -> None:
        with self._lock:
            i = self._need(identity_id)
            for s in i["samples"]:
                self._drop_files(s["id"])
            del self._ids[identity_id]
            self._save()

    def _drop_files(self, sid: str) -> None:
        self._emb.pop(sid, None)
        for ext in ("npy", "jpg"):
            try:
                self._sample_path(sid, ext).unlink()
            except FileNotFoundError:
                pass

    def add_sample(self, identity_id: str, embedding: np.ndarray, thumb_jpeg: bytes | None = None) -> str:
        emb = np.asarray(embedding, dtype=np.float32).ravel()
        if emb.size < 8 or not np.all(np.isfinite(emb)) or float(np.linalg.norm(emb)) < 1e-9:
            raise GalleryError("huella no válida")
        emb = emb / np.linalg.norm(emb)                # siempre unitaria: así el producto punto es el coseno
        with self._lock:
            i = self._need(identity_id)
            if len(i["samples"]) >= MAX_SAMPLES:
                raise GalleryError(f"límite de {MAX_SAMPLES} muestras por identidad")
            if i["samples"] and self._emb[i["samples"][0]["id"]].size != emb.size:
                raise GalleryError("la huella no tiene el tamaño de las muestras existentes (¿cambió el modelo?)")
            sid = secrets.token_hex(6)
            buf = io.BytesIO()
            np.save(buf, emb, allow_pickle=False)
            self._write_atomic(self._sample_path(sid, "npy"), buf.getvalue())
            if thumb_jpeg:
                self._write_atomic(self._sample_path(sid, "jpg"), thumb_jpeg)
            self._emb[sid] = emb
            i["samples"].append({"id": sid, "t": time.time()})
            self._save()
            return sid

    def delete_sample(self, identity_id: str, sample_id: str) -> None:
        with self._lock:
            i = self._need(identity_id)
            if not any(s["id"] == sample_id for s in i["samples"]):
                raise GalleryError(f"no existe la muestra {sample_id!r}")
            i["samples"] = [s for s in i["samples"] if s["id"] != sample_id]
            self._drop_files(sample_id)
            self._save()

    # --- reconocimiento ------------------------------------------------------------------------------------
    def samples_of(self, identity_id: str) -> list[np.ndarray]:
        with self._lock:
            i = self._ids.get(identity_id)
            return [] if i is None else [self._emb[s["id"]] for s in i["samples"]]

    def match(self, kind: str, embedding: np.ndarray, threshold: float, margin: float) -> Match:
        q = np.asarray(embedding, dtype=np.float32).ravel()
        n = float(np.linalg.norm(q))
        if q.size == 0 or not np.isfinite(n) or n < 1e-9:
            return Match(False, None, None, 0.0, 0.0)
        q = q / n
        scores: list[tuple[float, str]] = []
        with self._lock:
            for i in self._ids.values():
                if i["kind"] != kind or not i["samples"]:
                    continue
                m = np.stack([self._emb[s["id"]] for s in i["samples"]])
                if m.shape[1] != q.size:
                    continue
                sims = np.sort(m @ q)[::-1][:TOP_K]
                scores.append((float(sims.mean()), i["id"]))
        if not scores:
            return Match(False, None, None, 0.0, 0.0)
        scores.sort(reverse=True)
        best, best_id = scores[0]
        second = scores[1][0] if len(scores) > 1 else 0.0
        ok = best >= threshold and (len(scores) == 1 or best - second >= margin)
        return Match(ok, best_id if ok else None, best_id, best, second)
