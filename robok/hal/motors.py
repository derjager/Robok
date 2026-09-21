"""Motores: driver TB6612FNG sobre una abstracción de GPIO (lgpio real o falso) y motores simulados.

Convención: velocidades en [-1, 1] (+ = adelante). El límite de potencia (max_duty), las rampas y el
watchdog los aplica `services/drive.py`; este módulo solo traduce velocidad -> pines, y siempre deja
el driver en un estado seguro (STBY bajo, PWM en 0) al iniciar, al parar y al cerrar.
"""
from __future__ import annotations

import logging
import math
import threading
from typing import Protocol

from robok.config import MotorsCfg, Pins

log = logging.getLogger(__name__)

EPS = 1e-3   # por debajo de esto una velocidad cuenta como cero


class Motors(Protocol):
    def set_speeds(self, left: float, right: float) -> None: ...
    def stop(self) -> None: ...
    def close(self) -> None: ...


class GpioBackend(Protocol):
    def claim_output(self, pin: int, level: int = 0) -> None: ...
    def write(self, pin: int, level: int) -> None: ...
    def pwm(self, pin: int, hz: float, duty_pct: float) -> None: ...
    def close(self) -> None: ...


class LgpioBackend:
    """GPIO con la librería lgpio (PWM por software). Importa lgpio solo al usarse."""

    def __init__(self, chip: int = 0):
        import lgpio  # noqa: PLC0415  (import tardío: no existe en máquinas de desarrollo)
        self._lg = lgpio
        self._h = lgpio.gpiochip_open(chip)
        self._pins: set[int] = set()
        self._pwm: set[int] = set()

    def claim_output(self, pin: int, level: int = 0) -> None:
        self._lg.gpio_claim_output(self._h, pin, level)
        self._pins.add(pin)

    def _pwm_off(self, pin: int) -> None:
        if pin in self._pwm:
            self._lg.tx_pwm(self._h, pin, 0, 0)
            self._pwm.discard(pin)

    def write(self, pin: int, level: int) -> None:
        self._pwm_off(pin)
        self._lg.gpio_write(self._h, pin, 1 if level else 0)

    def pwm(self, pin: int, hz: float, duty_pct: float) -> None:
        if duty_pct <= 0:
            self.write(pin, 0)
        elif duty_pct >= 100:
            self.write(pin, 1)
        else:
            r = self._lg.tx_pwm(self._h, pin, hz, duty_pct)
            if r < 0:
                raise RuntimeError(f"lgpio.tx_pwm falló en GPIO{pin}: código {r}")
            self._pwm.add(pin)

    def close(self) -> None:
        for pin in sorted(self._pins):
            try:
                self.write(pin, 0)
                self._lg.gpio_free(self._h, pin)
            except Exception:  # cerrar debe ser robusto
                log.exception("no se pudo liberar GPIO%d", pin)
        self._pins.clear()
        self._lg.gpiochip_close(self._h)


class Tb6612Motors:
    """Dos motores con un TB6612FNG. Canal A = oruga izquierda, canal B = derecha (ver PINOUT.md §4.2)."""

    def __init__(self, gpio: GpioBackend, pins: Pins, cfg: MotorsCfg):
        self._gpio, self._pins, self._cfg = gpio, pins, cfg
        self._lock = threading.Lock()
        self._standby_off = True
        self._closed = False
        a = (pins.motor_pwma, pins.motor_ain1, pins.motor_ain2)
        b = (pins.motor_pwmb, pins.motor_bin1, pins.motor_bin2)
        self._left_ch, self._right_ch = (b, a) if cfg.swap_sides else (a, b)
        # estado seguro ANTES de cualquier otra cosa: STBY primero, luego el resto, todo en bajo
        gpio.claim_output(pins.motor_stby, 0)
        for pin in (*a, *b):
            gpio.claim_output(pin, 0)
        self.last = (0.0, 0.0)

    def _drive(self, ch: tuple[int, int, int], speed: float, invert: bool) -> None:
        pwm, in1, in2 = ch
        if invert:
            speed = -speed
        if abs(speed) < EPS:
            self._gpio.pwm(pwm, self._cfg.pwm_hz, 0)
            self._gpio.write(in1, 0)
            self._gpio.write(in2, 0)
            return
        self._gpio.write(in1, 1 if speed > 0 else 0)
        self._gpio.write(in2, 0 if speed > 0 else 1)
        self._gpio.pwm(pwm, self._cfg.pwm_hz, min(1.0, abs(speed)) * 100.0)

    def set_speeds(self, left: float, right: float) -> None:
        if not (math.isfinite(left) and math.isfinite(right)):
            self.stop()
            raise ValueError("velocidad no finita")
        left, right = max(-1.0, min(1.0, left)), max(-1.0, min(1.0, right))
        with self._lock:
            if self._closed:
                return
            if abs(left) < EPS and abs(right) < EPS:
                self._stop_locked()
                return
            # dirección y PWM primero; STBY se sube al final para no tener un pulso espurio
            self._drive(self._left_ch, left, self._cfg.invert_left)
            self._drive(self._right_ch, right, self._cfg.invert_right)
            if self._standby_off:
                self._gpio.write(self._pins.motor_stby, 1)
                self._standby_off = False
            self.last = (left, right)

    def _stop_locked(self) -> None:
        self._gpio.write(self._pins.motor_stby, 0)     # STBY primero: el driver se desconecta al instante
        self._standby_off = True
        for ch in (self._left_ch, self._right_ch):
            self._drive(ch, 0.0, False)
        self.last = (0.0, 0.0)

    def stop(self) -> None:
        with self._lock:
            if not self._closed:
                self._stop_locked()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._stop_locked()
            self._closed = True
        self._gpio.close()


class FakeMotors:
    """Motores simulados: guardan las órdenes. Para pruebas, modo --sim y motors.enabled = false."""

    def __init__(self) -> None:
        self.left = 0.0
        self.right = 0.0
        self.history: list[tuple[float, float]] = []
        self.stops = 0
        self.closed = False
        self._lock = threading.Lock()

    def set_speeds(self, left: float, right: float) -> None:
        if not (math.isfinite(left) and math.isfinite(right)):
            raise ValueError("velocidad no finita")
        with self._lock:
            self.left, self.right = max(-1.0, min(1.0, left)), max(-1.0, min(1.0, right))
            self.history.append((self.left, self.right))

    def stop(self) -> None:
        with self._lock:
            self.left = self.right = 0.0
            self.stops += 1

    def close(self) -> None:
        self.stop()
        self.closed = True


def infer_wiring(t1: str, d1: str, t2: str, d2: str) -> dict | None:
    """Deduce la corrección de cableado a partir de la prueba de puesta en marcha.

    Con swap=False e invert=False se mandó "izquierda adelante" (t1, d1) y "derecha adelante" (t2, d2),
    donde t = oruga física que se movió ('i' izquierda, 'd' derecha) y d = su sentido ('a' adelante,
    'r' atrás). Devuelve {"swap_sides", "invert_left", "invert_right"} para config.toml, o None si las
    respuestas no permiten concluir (una oruga no se movió, o se movió la misma dos veces).
    """
    if {t1, t2} != {"i", "d"} or d1 not in "ar" or d2 not in "ar" or len(d1) != 1 or len(d2) != 1:
        return None
    swap = t1 == "d"                    # el canal A movió la oruga derecha: hay que cruzar los lados
    dir_a, dir_b = d1, d2               # sentido observado del canal A y del canal B
    left_dir, right_dir = (dir_b, dir_a) if swap else (dir_a, dir_b)
    return {"swap_sides": swap, "invert_left": left_dir == "r", "invert_right": right_dir == "r"}
