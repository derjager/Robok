"""Reglas de seguridad como funciones puras (sin hilos ni hardware), para probarlas a fondo.

Trabajan en el espacio de las orugas: izquierda/derecha en [-1, 1] (+ = adelante). El avance del
robot es v = (l + r) / 2 y el giro w = (l - r) / 2; limitar el avance no debe anular el giro.
"""
from __future__ import annotations

import math

from robok.config import SafetyCfg


def clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def require_finite(*values: float) -> None:
    for v in values:
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
            raise ValueError(f"valor no válido (debe ser un número finito): {v!r}")


def deadzone(v: float, dz: float) -> float:
    """Zona muerta con reescalado: [-dz, dz] -> 0 y el resto se estira a [-1, 1] sin saltos."""
    if abs(v) <= dz:
        return 0.0
    return math.copysign((abs(v) - dz) / (1.0 - dz), v)


def mix_tank(x: float, y: float) -> tuple[float, float]:
    """Joystick (x derecha, y adelante) -> (izquierda, derecha), conservando la proporción si satura."""
    left, right = y + x, y - x
    peak = max(abs(left), abs(right), 1.0)
    return left / peak, right / peak


def limit_obstacles(left: float, right: float, front_cm: float | None, rear_cm: float | None,
                    cfg: SafetyCfg) -> tuple[float, float, str | None]:
    """Recorta el avance según los ToF. Devuelve (izq, der, motivo). None = sin lectura = sin límite.

    - Delante: por debajo de front_slow_cm el avance baja linealmente; a front_stop_cm o menos se anula.
    - Detrás: a rear_stop_cm o menos se anula la reversa.
    El giro se conserva, así el robot puede girar para librarse del obstáculo.
    """
    v, w = (left + right) / 2.0, (left - right) / 2.0
    reason = None
    if v > 0 and front_cm is not None:
        if front_cm <= cfg.front_stop_cm:
            v, reason = 0.0, "obstaculo_frente"
        elif front_cm < cfg.front_slow_cm:
            v *= (front_cm - cfg.front_stop_cm) / (cfg.front_slow_cm - cfg.front_stop_cm)
            reason = "obstaculo_frente_cerca"
    elif v < 0 and rear_cm is not None and rear_cm <= cfg.rear_stop_cm:
        v, reason = 0.0, "obstaculo_atras"
    return clamp(v + w), clamp(v - w), reason


def slew(current: float, target: float, max_step: float) -> float:
    """Acerca `current` a `target` sin cambiar más de `max_step` en este paso."""
    delta = target - current
    if abs(delta) <= max_step:
        return target
    return current + math.copysign(max_step, delta)


def brake_slew(current: float, target: float, max_step: float) -> float:
    """Como `slew`, pero para frenadas de seguridad: reducir potencia es inmediato y un cambio de sentido
    pasa primero por cero (nunca invierte una oruga de golpe); acelerar sigue usando la rampa."""
    if current == 0 or (target != 0 and (target > 0) == (current > 0)):
        return target if abs(target) <= abs(current) else slew(current, target, max_step)
    return 0.0
