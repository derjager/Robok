"""Obstáculos: convierte las lecturas de los ToF en las dos distancias que entiende el Drive (frente y atrás).

Reglas (fail-safe: ante la duda, se considera que hay un obstáculo pegado):
  - Delante cuenta el más cercano de los sensores frontales listados; detrás, el trasero.
  - "libre" no limita; "ok" limita por su distancia.
  - Un sensor listado en `tof.sensors` que está en "error" o "iniciando" cuenta como 0 cm (bloquea esa dirección).
    Lo que no se instaló no se lista, y entonces no cuenta.
  - Se repite `set_obstacles` en cada ciclo: si este hilo muere, el Drive lo nota (safety.sensor_timeout_s) y bloquea.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from robok.config import TOF_FRONT, SafetyCfg
from robok.hal.tof import Tof, TofReading
from robok.services.drive import Drive

log = logging.getLogger(__name__)

REAR = ("rear",)


def direction_cm(readings: dict[str, TofReading], names: tuple[str, ...]) -> tuple[float | None, list[str]]:
    """(distancia más corta o None si despejado, sensores en falla) para un grupo de sensores."""
    best: float | None = None
    faults: list[str] = []
    for n in names:
        r = readings.get(n)
        if r is None:
            continue                       # ese sensor no está instalado
        if r.state == "libre":
            continue
        if r.state == "ok" and r.cm is not None:
            best = r.cm if best is None else min(best, r.cm)
        else:
            faults.append(n)
    return (0.0 if faults else best), faults


class Obstacles:
    def __init__(self, tof: Tof, drive: Drive, hz: float = 20.0,
                 on_event: Callable[[str], None] | None = None, safety: SafetyCfg | None = None):
        self.tof, self.drive, self._hz, self._on_event = tof, drive, hz, on_event
        self._safety = safety or SafetyCfg()
        self._lock = threading.Lock()
        self._front: float | None = None
        self._rear: float | None = None
        self._faults: list[str] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def update(self) -> None:
        """Un ciclo: lee los sensores y actualiza el Drive."""
        readings = self.tof.snapshot()
        front, f_front = direction_cm(readings, TOF_FRONT)
        rear, f_rear = direction_cm(readings, REAR)
        faults = f_front + f_rear
        with self._lock:
            new_fault = bool(faults) and not self._faults
            recovered = not faults and bool(self._faults)
            self._front, self._rear, self._faults = front, rear, faults
        self.drive.set_obstacles(front, rear)
        if new_fault:
            log.warning("sensores ToF con falla: %s (se bloquea su dirección)", ", ".join(faults))
            self._emit("sensor_fallo")
        elif recovered:
            log.info("sensores ToF recuperados")
            self._emit("sensor_ok")

    def _emit(self, kind: str) -> None:
        if self._on_event:
            try:
                self._on_event(kind)
            except Exception:
                log.exception("error en el manejador del evento %s", kind)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="obstacles", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        period = 1.0 / self._hz
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self.update()
            except Exception:
                # sin llamar a set_obstacles, el Drive detecta el silencio y bloquea; aquí solo se deja constancia
                log.exception("fallo en el servicio de obstáculos")
            self._stop.wait(max(0.0, period - (time.monotonic() - t0)))

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    def status(self) -> dict:
        readings = self.tof.snapshot()
        with self._lock:
            front, rear, faults = self._front, self._rear, list(self._faults)
        return {"desc": self.tof.desc, "front_cm": front, "rear_cm": rear, "faults": faults,
                "stop_cm": self._safety.front_stop_cm, "slow_cm": self._safety.front_slow_cm,
                "rear_stop_cm": self._safety.rear_stop_cm,
                "sensors": {n: r.as_dict() for n, r in readings.items()}}
