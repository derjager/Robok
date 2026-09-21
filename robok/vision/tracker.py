"""Seguimiento de objetos entre cuadros: mantiene el mismo id para el mismo gato/persona y acumula votos de identidad.

Asociación por IoU (voraz, mismo tipo). A 4 cuadros/s un gato se mueve bastante, así que se acepta un IoU bajo
y además se admite asociar por cercanía de centros cuando las cajas son parecidas en tamaño.

Identidad estable: se decide por votos de las últimas lecturas (no por una sola), con histéresis para no
parpadear entre dos nombres. Las personas mantienen su identidad `hold_s` segundos aunque la cara deje de verse.
"""
from __future__ import annotations

import collections
import itertools
import math
from dataclasses import dataclass, field

from robok.vision.types import Box, Detection, iou

IOU_MIN = 0.15
CENTER_DIST_MAX = 0.6       # en múltiplos del lado mayor de la caja
VOTES = 6


@dataclass
class Track:
    id: int
    kind: str
    box: Box
    score: float
    first_seen: float
    last_seen: float
    hits: int = 1
    votes: collections.deque = field(default_factory=lambda: collections.deque(maxlen=VOTES))   # (identity_id|None, score)
    identity_id: str | None = None
    identity_score: float = 0.0
    identity_at: float = 0.0          # última vez que se decidió/confirmó la identidad
    last_vote_at: float = 0.0
    face_seen_at: float = 0.0
    cand_id: str | None = None        # mejor candidato de la última lectura, se haya aceptado o no (para mostrar la puntuación)
    cand_score: float = 0.0
    updated: bool = False             # ¿vino en el último cuadro?

    @property
    def center(self) -> tuple[float, float]:
        return ((self.box[0] + self.box[2]) / 2, (self.box[1] + self.box[3]) / 2)

    def add_vote(self, identity_id: str | None, score: float, now: float) -> None:
        self.votes.append((identity_id, score))
        self.last_vote_at = now
        tally: dict[str, list[float]] = {}
        for ident, sc in self.votes:
            if ident is not None:
                tally.setdefault(ident, []).append(sc)
        if not tally:
            if len(self.votes) >= 3:                       # 3 lecturas sin reconocer: ya no se sabe quién es
                self.identity_id, self.identity_score = None, 0.0
            return
        best = max(tally, key=lambda k: (len(tally[k]), sum(tally[k]) / len(tally[k])))
        # histéresis: se cambia de nombre solo si el retador tiene estrictamente más votos
        cur = self.identity_id
        if cur is not None and cur in tally and cur != best and len(tally[best]) <= len(tally[cur]):
            best = cur
        if len(tally[best]) >= 2 or self.identity_id == best:
            self.identity_id = best
            self.identity_score = sum(tally[best]) / len(tally[best])
            self.identity_at = now


class Tracker:
    def __init__(self, max_age_s: float = 1.2, hold_s: float = 8.0):
        self.max_age_s, self.hold_s = max_age_s, hold_s
        self._ids = itertools.count(1)
        self.tracks: list[Track] = []

    def update(self, dets: list[Detection], now: float) -> list[Track]:
        for t in self.tracks:
            t.updated = False
        pairs = []
        for t in self.tracks:
            for i, d in enumerate(dets):
                if d.kind != t.kind:
                    continue
                v = iou(t.box, d.box)
                if v < IOU_MIN:
                    v = self._closeness(t.box, d.box)
                if v > 0:
                    pairs.append((v, t.id, i))
        used_t: set[int] = set()
        used_d: set[int] = set()
        by_id = {t.id: t for t in self.tracks}
        for v, tid, i in sorted(pairs, reverse=True):
            if tid in used_t or i in used_d:
                continue
            used_t.add(tid); used_d.add(i)
            t, d = by_id[tid], dets[i]
            t.box, t.score, t.last_seen, t.hits, t.updated = d.box, d.score, now, t.hits + 1, True
        for i, d in enumerate(dets):
            if i not in used_d:
                self.tracks.append(Track(next(self._ids), d.kind, d.box, d.score, now, now, updated=True))
        limit = {"person": self.hold_s}
        self.tracks = [t for t in self.tracks if now - t.last_seen <= limit.get(t.kind, self.max_age_s)
                       or (t.updated)]
        return [t for t in self.tracks if t.updated]

    @staticmethod
    def _closeness(a: Box, b: Box) -> float:
        """Puntuación (0..0.15) si los centros están cerca y las cajas tienen tamaño parecido; 0 si no."""
        (ax, ay), (bx, by) = ((a[0] + a[2]) / 2, (a[1] + a[3]) / 2), ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
        side = max(a[2] - a[0], a[3] - a[1], b[2] - b[0], b[3] - b[1])
        wa, ha, wb, hb = a[2] - a[0], a[3] - a[1], b[2] - b[0], b[3] - b[1]
        ratio = min(wa * ha, wb * hb) / max(wa * ha, wb * hb, 1e-9)
        dist = math.hypot(ax - bx, ay - by) / max(side, 1e-9)
        if ratio < 0.4 or dist > CENTER_DIST_MAX:
            return 0.0
        return 0.14 * (1 - dist / CENTER_DIST_MAX) + 1e-6
