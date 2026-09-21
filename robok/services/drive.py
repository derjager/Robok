"""Control de movimiento: joystick -> tanque, con todas las protecciones entre el usuario y los motores.

Orden de las protecciones (de la orden hacia el pin):
  paro de emergencia > watchdog > límite por obstáculos > rampa de aceleración > tope de potencia.
Regla de oro: cualquier fallo o duda termina en motores parados (fail-safe).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from robok.config import MotorsCfg, SafetyCfg
from robok.hal.motors import Motors
from robok.services.safety import (brake_slew, clamp, deadzone, limit_obstacles, mix_tank, require_finite,
                                   slew)

log = logging.getLogger(__name__)

LOOP_HZ = 25
# Motivos que frenan de inmediato (sin rampa): la seguridad no espera a que "termine de desacelerar".
IMMEDIATE = {"estop", "watchdog", "obstaculo_frente", "obstaculo_atras"}


class Drive:
    def __init__(self, motors: Motors, cfg: MotorsCfg, safety: SafetyCfg,
                 clock: Callable[[], float] = time.monotonic,
                 on_event: Callable[[str], None] | None = None):
        self.motors, self.cfg, self.safety = motors, cfg, safety
        self._clock = clock
        self._on_event = on_event
        self._lock = threading.RLock()
        self._target = (0.0, 0.0)       # orden del usuario ya mezclada, en [-1, 1]
        self._cur = (0.0, 0.0)          # valor tras rampa, en [-1, 1] (antes del tope max_duty)
        self._last_cmd = clock()
        self._last_tick = clock()
        self._estop = False
        self._front: float | None = None
        self._rear: float | None = None
        self._obst_at = clock()         # cuándo llegó el último dato de obstáculos
        self._obst_live = False         # ya hay un servicio de sensores alimentándolos: su silencio es una falla
        self._obst_stale = False
        self._reason: str | None = None
        self._stale = False
        self._last_out = (0.0, 0.0)
        self._stopped = False           # el driver ya está en estado seguro (evita reescribir GPIO en cada ciclo)
        self._stopped_at = 0.0
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()

    # --- órdenes -------------------------------------------------------------------------
    def command(self, x: float, y: float, scale: float = 1.0) -> bool:
        """Joystick: x giro (+ derecha), y avance (+ adelante), scale 0..1 (límite de velocidad del usuario).

        Cualquier orden (también una de parar) renueva el watchdog. Devuelve False si hay paro de emergencia.
        """
        try:
            require_finite(x, y, scale)
        except ValueError:
            self.stop()
            raise
        x, y, scale = clamp(x), clamp(y), clamp(scale, 0.0, 1.0)
        dz = self.cfg.deadzone
        left, right = mix_tank(deadzone(x, dz), deadzone(y, dz))
        return self._set_target(left * scale, right * scale)

    def command_tracks(self, left: float, right: float) -> bool:
        """Orden directa por oruga (para comportamientos autónomos)."""
        require_finite(left, right)
        return self._set_target(clamp(left), clamp(right))

    def _set_target(self, left: float, right: float) -> bool:
        with self._lock:
            self._last_cmd = self._clock()
            if self._estop:
                return False
            self._target = (left, right)
            return True

    def stop(self) -> None:
        """Para de inmediato (sin rampa) y borra la orden."""
        with self._lock:
            self._target = self._cur = (0.0, 0.0)
            self._last_cmd = self._clock()
        self._apply(0.0, 0.0)

    def estop(self) -> None:
        """Paro de emergencia: para ya y queda bloqueado hasta `reset_estop()`."""
        with self._lock:
            already = self._estop
            self._estop = True
            self._target = self._cur = (0.0, 0.0)
        self.motors.stop()
        self._last_out = (0.0, 0.0)
        self._stopped, self._stopped_at = True, time.monotonic()
        if not already:
            self._emit("estop")

    def reset_estop(self) -> bool:
        with self._lock:
            if not self._estop:
                return True
            self._estop = False
            self._target = self._cur = (0.0, 0.0)
            self._last_cmd = self._clock()
        self._emit("estop_reset")
        return True

    def set_obstacles(self, front_cm: float | None, rear_cm: float | None) -> None:
        """Distancias de los ToF en cm (None = despejado). Quien lo llame debe repetirlo unas veces por segundo:
        tras `sensor_timeout_s` sin llamadas se asume que los sensores fallaron y se bloquea avance y reversa."""
        with self._lock:
            self._front, self._rear = front_cm, rear_cm
            self._obst_at, self._obst_live = self._clock(), True

    # --- bucle de control ------------------------------------------------------------------
    def tick(self, now: float | None = None) -> tuple[float, float]:
        """Un paso del control. Devuelve la potencia aplicada (izq, der) ya con el tope max_duty."""
        now = self._clock() if now is None else now
        events: list[str] = []
        with self._lock:
            dt = max(0.0, min(now - self._last_tick, 0.5))
            self._last_tick = now
            reason: str | None = None
            target = self._target
            if self._estop:
                target, reason = (0.0, 0.0), "estop"
            elif now - self._last_cmd > self.cfg.watchdog_s:
                if any(abs(t) > 1e-6 for t in target) or self._cur != (0.0, 0.0):
                    reason = "watchdog"
                target = self._target = (0.0, 0.0)
                self._stale = True
            else:
                self._stale = False
                target = self._target
                front, rear = self._front, self._rear
                self._obst_stale = self._obst_live and now - self._obst_at > self.safety.sensor_timeout_s
                if self._obst_stale:     # fail-safe: sin datos de los sensores no se sabe qué hay delante ni detrás
                    front = rear = 0.0
                l, r, obst = limit_obstacles(*target, front, rear, self.safety)
                target, reason = (l, r), obst

            step = self.cfg.ramp_per_s * dt
            cur = self._cur
            if reason in IMMEDIATE:
                # frena de golpe (sin invertir de golpe); acelerar sigue por rampa, p. ej. un giro junto a un obstáculo
                cur = tuple(brake_slew(c, t, step) for c, t in zip(cur, target))
            else:
                cur = tuple(slew(c, t, step) for c, t in zip(cur, target))
            self._cur = (cur[0], cur[1])
            if reason != self._reason and reason in {"watchdog", "obstaculo_frente", "obstaculo_atras"}:
                events.append(reason)
            self._reason = reason
            out = (self._cur[0] * self.cfg.max_duty, self._cur[1] * self.cfg.max_duty)
        self._apply(*out)
        for e in events:
            self._emit(e)
        return out

    def _apply(self, left: float, right: float) -> None:
        self._last_out = (left, right)
        if abs(left) < 1e-6 and abs(right) < 1e-6:
            now = time.monotonic()
            if not self._stopped or now - self._stopped_at > 1.0:   # repite el estado seguro cada segundo
                self.motors.stop()
                self._stopped, self._stopped_at = True, now
        else:
            self._stopped = False
            self.motors.set_speeds(left, right)

    def _emit(self, kind: str) -> None:
        if self._on_event:
            try:
                self._on_event(kind)
            except Exception:
                log.exception("error en el manejador del evento %s", kind)

    # --- hilo ----------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._last_tick = self._clock()
        self._thread = threading.Thread(target=self._run, name="drive", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop_evt.set()
        if self._thread:
            self._thread.join(timeout=2)
        try:
            self.stop()
        finally:
            self.motors.close()

    def _run(self) -> None:
        period = 1.0 / LOOP_HZ
        while not self._stop_evt.is_set():
            t0 = time.monotonic()
            try:
                self.tick()
            except Exception:
                # fail-safe: si el control falla, se para todo y se bloquea hasta que un humano lo reinicie
                log.exception("fallo en el bucle de control; paro de emergencia")
                try:
                    self.estop()
                except Exception:
                    log.exception("no se pudo ejecutar el paro de emergencia")
            self._stop_evt.wait(max(0.0, period - (time.monotonic() - t0)))

    # --- estado ----------------------------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            return {"left": round(self._last_out[0], 3), "right": round(self._last_out[1], 3),
                    "estop": self._estop, "stale": self._stale, "limited": self._reason,
                    "front_cm": self._front, "rear_cm": self._rear, "obstacles_stale": self._obst_stale,
                    "max_duty": self.cfg.max_duty}
