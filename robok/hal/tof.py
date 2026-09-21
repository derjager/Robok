"""Sensores de distancia ToF (VL53L0X ×4): arranque con XSHUT, lectura continua y filtrado.

De fábrica todos los VL53L0X responden en 0x29. Para separarlos, al arrancar se bajan los XSHUT de los cuatro
(quedan apagados) y se enciende uno por uno: cada uno se inicializa en 0x29 y enseguida se le asigna su
dirección (0x30 + índice). La dirección se pierde si se le baja el XSHUT, y así también se recupera un
sensor colgado: se apaga y se vuelve a levantar solo ese.

Un hilo lee todos a `poll_hz`. Cada lectura pasa por una mediana de 3 para descartar picos sueltos.
Estados de un sensor: "ok" (hay distancia), "libre" (nada dentro de max_cm), "iniciando" y "error"
(no responde o hace más de stale_s que no entrega un dato nuevo).
"""
from __future__ import annotations

import collections
import logging
import statistics
import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from robok.config import TOF_NAMES, Pins, TofCfg
from robok.hal.motors import GpioBackend, LgpioBackend
from robok.hal.vl53l0x import DEFAULT_ADDRESS, NO_TARGET_MM, VL53L0X, I2cBus, SmbusI2c, Vl53Error

log = logging.getLogger(__name__)

RECOVER_AFTER_S = 1.0       # un sensor callado más de esto se reinicia (solo él)
RETRY_S = (2.0, 5.0, 10.0)  # espera entre reintentos de arranque, creciente


@dataclass(frozen=True)
class TofReading:
    name: str
    state: str                   # ok | libre | iniciando | error
    cm: float | None = None      # solo con state == "ok"
    age_s: float | None = None   # antigüedad del último dato válido

    def as_dict(self) -> dict:
        return {"state": self.state, "cm": self.cm, "age_s": None if self.age_s is None else round(self.age_s, 2)}


class Tof(Protocol):
    names: tuple[str, ...]
    desc: str

    def start(self) -> None: ...
    def snapshot(self) -> dict[str, TofReading]: ...
    def close(self) -> None: ...


class _Sensor:
    def __init__(self, name: str, index: int, xshut: int, address: int):
        self.name, self.index, self.xshut, self.address = name, index, xshut, address
        self.dev: VL53L0X | None = None
        self.window: collections.deque[float] = collections.deque(maxlen=3)   # mm, inf = sin eco
        self.last_ok: float | None = None
        self.started_at = 0.0
        self.errors = 0
        self.failed = False
        self.error = ""
        self.retry_at = 0.0
        self.retries = 0


class TofArray:
    def __init__(self, cfg: TofCfg, pins: Pins, *, bus_factory: Callable[[int], I2cBus] = SmbusI2c,
                 gpio_factory: Callable[[int], GpioBackend] = LgpioBackend,
                 clock: Callable[[], float] = time.monotonic, settle_s: float = 0.02):
        self.cfg, self._pins = cfg, pins
        self.names = tuple(cfg.sensors)
        self.desc = f"VL53L0X ×{len(self.names)} (I2C{cfg.i2c_bus}, 0x{cfg.base_address:02X}+)"
        self._bus_factory, self._gpio_factory, self._clock, self._settle = bus_factory, gpio_factory, clock, settle_s
        self._bus: I2cBus | None = None
        self._gpio: GpioBackend | None = None
        self._lock = threading.Lock()
        self._fatal = ""
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._sensors = {n: _Sensor(n, TOF_NAMES.index(n), pins.tof_xshut[TOF_NAMES.index(n)],
                                    cfg.base_address + TOF_NAMES.index(n)) for n in self.names}

    # --- arranque y parada ----------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        try:
            self._bus = self._bus_factory(self.cfg.i2c_bus)
            self._gpio = self._gpio_factory(self.cfg.gpiochip)
            for pin in self._pins.tof_xshut:      # los 4 apagados: incluso los no listados, por si están cableados
                self._gpio.claim_output(pin, 0)
        except Exception as e:
            self._fatal = f"{type(e).__name__}: {e}"
            log.error("ToF: no se pudo abrir el I2C/GPIO (%s); los sensores quedan en error", self._fatal)
            self._release()
            return
        time.sleep(self._settle)
        now = self._clock()
        for s in self._sensors.values():
            s.started_at = now
            self._bring_up(s)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="tof", daemon=True)
        self._thread.start()

    def _bring_up(self, s: _Sensor) -> bool:
        """Apaga y enciende SOLO este sensor, lo inicializa en 0x29 y le asigna su dirección."""
        assert self._bus is not None and self._gpio is not None
        s.dev = None
        try:
            self._gpio.write(s.xshut, 0)
            time.sleep(self._settle)
            self._gpio.write(s.xshut, 1)
            time.sleep(self._settle)
            dev = VL53L0X(self._bus, DEFAULT_ADDRESS)
            dev.set_address(s.address)
            if not dev.ident_ok():
                raise Vl53Error(f"no responde en la dirección nueva 0x{s.address:02X}")
            dev.start_continuous()
        except Exception as e:
            s.failed, s.error = True, f"{type(e).__name__}: {e}"
            s.retry_at = self._clock() + RETRY_S[min(s.retries, len(RETRY_S) - 1)]
            s.retries += 1
            try:
                self._gpio.write(s.xshut, 0)    # apagado: que no estorbe a otro sensor en 0x29
            except Exception:
                log.exception("ToF %s: no se pudo apagar el XSHUT", s.name)
            log.warning("ToF %s: no arrancó (%s)", s.name, s.error)
            return False
        with self._lock:
            s.dev, s.failed, s.error, s.errors, s.retries = dev, False, "", 0, 0
            s.window.clear()
            s.last_ok, s.started_at = None, self._clock()
        log.info("ToF %s: listo en 0x%02X", s.name, s.address)
        return True

    def _release(self) -> None:
        for res in (self._gpio, self._bus):
            try:
                if res is not None:
                    res.close()
            except Exception:
                log.exception("ToF: error al liberar recursos")
        self._gpio = self._bus = None

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        if self._gpio is not None:
            for s in self._sensors.values():
                if s.dev is not None:
                    try:
                        s.dev.stop_continuous()
                    except Exception:
                        log.debug("ToF %s: no se pudo detener la medición", s.name)
                    s.dev = None
            for pin in self._pins.tof_xshut:      # sensores apagados al salir
                try:
                    self._gpio.write(pin, 0)
                except Exception:
                    log.exception("ToF: no se pudo bajar el XSHUT %d", pin)
        self._release()

    # --- lectura -------------------------------------------------------------------------------
    def _run(self) -> None:
        period = 1.0 / self.cfg.poll_hz
        while not self._stop.is_set():
            t0 = time.monotonic()
            for s in self._sensors.values():
                if self._stop.is_set():
                    return
                try:
                    self._poll(s)
                except Exception:
                    log.exception("ToF %s: fallo inesperado", s.name)
            self._stop.wait(max(0.0, period - (time.monotonic() - t0)))

    def _poll(self, s: _Sensor) -> None:
        now = self._clock()
        if s.dev is None:                                   # arranque fallido: reintento con espera creciente
            if now >= s.retry_at:
                self._bring_up(s)
            return
        try:
            mm = s.dev.read_mm()
        except Vl53Error as e:
            s.errors += 1
            s.error = str(e)
            if s.errors >= 3:
                s.failed = True
            mm = None
        if mm is not None:
            with self._lock:
                s.window.append(float("inf") if mm >= NO_TARGET_MM else float(mm))
                s.last_ok, s.errors, s.failed = now, 0, False
            return
        silent = now - (s.last_ok if s.last_ok is not None else s.started_at)
        if s.failed or silent > RECOVER_AFTER_S:
            s.failed = True
            log.warning("ToF %s: sin datos (%s); se reinicia", s.name, s.error or f"callado {silent:.1f} s")
            s.retries = 0
            self._bring_up(s)

    def snapshot(self) -> dict[str, TofReading]:
        now = self._clock()
        out: dict[str, TofReading] = {}
        with self._lock:
            for name, s in self._sensors.items():
                if self._fatal or s.failed:
                    out[name] = TofReading(name, "error")
                elif s.last_ok is None:
                    out[name] = TofReading(name, "iniciando")
                else:
                    age = now - s.last_ok
                    if age > self.cfg.stale_s:
                        out[name] = TofReading(name, "error", None, age)
                        continue
                    mm = statistics.median(s.window)
                    if mm == float("inf") or mm / 10 > self.cfg.max_cm:
                        out[name] = TofReading(name, "libre", None, age)
                    else:
                        out[name] = TofReading(name, "ok", round(mm / 10, 1), age)
        return out

    def problems(self) -> dict[str, str]:
        """Motivo del fallo de cada sensor en error (para la puesta en marcha y el log)."""
        return {n: (self._fatal or s.error) for n, s in self._sensors.items() if self._fatal or s.failed}


class FakeTof:
    """ToF simulado: las distancias se fijan a mano (`set`) para probar el límite de obstáculos."""

    def __init__(self, names: tuple[str, ...] = TOF_NAMES, max_cm: float = 120.0):
        self.names = tuple(names)
        self.desc = "simulados"
        self.max_cm = max_cm
        self._state: dict[str, tuple[str, float | None]] = {n: ("libre", None) for n in self.names}
        self.started = self.closed = False

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True

    def set(self, name: str, cm: float | None) -> None:
        """cm=None: nada a la vista. Más lejos que max_cm también cuenta como libre."""
        if name not in self._state:
            raise KeyError(name)
        if cm is None or cm > self.max_cm:
            self._state[name] = ("libre", None)
        elif cm < 0 or cm != cm:
            raise ValueError(f"distancia no válida: {cm!r}")
        else:
            self._state[name] = ("ok", float(cm))

    def fail(self, name: str, state: str = "error") -> None:
        if name not in self._state:
            raise KeyError(name)
        self._state[name] = (state, None)

    def snapshot(self) -> dict[str, TofReading]:
        return {n: TofReading(n, st, cm, None if st in {"iniciando", "error"} else 0.0)
                for n, (st, cm) in self._state.items()}

    def problems(self) -> dict[str, str]:
        return {n: "simulado" for n, (st, _) in self._state.items() if st == "error"}


def create_tof(cfg: TofCfg, pins: Pins, sim: bool) -> Tof | None:
    """None si los ToF están desactivados. Con --sim son simulados; si no, reales (y si fallan, dan error
    y bloquean el avance: nunca se sustituyen por simulados)."""
    if not cfg.enabled:
        return None
    if sim:
        return FakeTof(cfg.sensors, cfg.max_cm)
    return TofArray(cfg, pins)
