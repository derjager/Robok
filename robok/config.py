"""Configuración de Wally: valores por defecto en código, sobrescritos por config.toml.

Los pines (numeración BCM) son la fuente de verdad del código; el cableado físico está en
PINOUT.md y en docs/make_wiring_svg.py. Si cambias uno, cámbialo en los tres.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass, replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.toml"


@dataclass(frozen=True)
class Pins:
    # LCD (SPI0 + DC + RST). Los gestiona el overlay del kernel; se listan solo como referencia.
    lcd_rst: int = 27
    lcd_dc: int = 22
    lcd_cs: int = 8
    lcd_mosi: int = 10
    lcd_sclk: int = 11
    # I2C1 hardware
    i2c_sda: int = 2
    i2c_scl: int = 3
    # TB6612FNG
    motor_pwma: int = 16
    motor_ain1: int = 19
    motor_ain2: int = 20
    motor_pwmb: int = 21
    motor_bin1: int = 23
    motor_bin2: int = 24
    motor_stby: int = 26
    # Servos (PWM por hardware)
    servo1: int = 12
    servo2: int = 13
    # XSHUT de ToF-1 .. ToF-4
    tof_xshut: tuple[int, ...] = (6, 5, 25, 4)

    def all_used(self) -> list[int]:
        pins: list[int] = []
        for f in fields(self):
            v = getattr(self, f.name)
            pins.extend(v if isinstance(v, tuple) else [v])
        return pins


@dataclass(frozen=True)
class DisplayCfg:
    framebuffer_name: str = "fb_ili9340"
    fps: int = 20            # frecuencia del bucle de la cara; el panel da ~13 fps a pantalla completa
    color_bgr: bool = False  # el overlay ya aplica el orden de color; solo para pruebas


@dataclass(frozen=True)
class VoiceCfg:
    voice: str = "es-419"
    speed: int = 150         # palabras por minuto de espeak-ng
    pitch: int = 50
    max_chars: int = 200
    enabled: bool = True


@dataclass(frozen=True)
class WebCfg:
    host: str = "0.0.0.0"
    port: int = 8000
    token: str = ""          # vacío: ROBOK_TOKEN o el archivo .robok_token (se genera solo)


@dataclass(frozen=True)
class MotorsCfg:
    enabled: bool = False       # SEGURO POR DEFECTO: sin esto los motores son simulados aunque no haya --sim
    backend: str = "lgpio"      # lgpio (PWM por software); pigpio queda pendiente (no existe en Trixie)
    gpiochip: int = 0
    pwm_hz: int = 1000
    max_duty: float = 0.4       # tope de PWM. 0.4 protege FA-130 (3 V) si VM = batería 7.4 V; con buck de 3 V puede ser 1.0
    ramp_per_s: float = 2.5     # aceleración máxima (fracción de la escala completa por segundo)
    watchdog_s: float = 0.5     # sin órdenes durante este tiempo, se paran los motores
    deadzone: float = 0.08      # zona muerta del joystick
    invert_left: bool = False   # invertir el sentido si la oruga gira al revés
    invert_right: bool = False
    swap_sides: bool = False    # intercambiar canal A y B (si izquierda y derecha quedaron al revés)

    def __post_init__(self) -> None:
        if self.backend not in {"lgpio"}:
            raise ValueError(f"motors.backend desconocido: {self.backend!r} (válido: lgpio)")
        if not 0.0 < self.max_duty <= 1.0:
            raise ValueError("motors.max_duty debe estar en (0, 1]")
        if not 100 <= self.pwm_hz <= 10000:
            raise ValueError("motors.pwm_hz debe estar entre 100 y 10000")
        if self.ramp_per_s <= 0:
            raise ValueError("motors.ramp_per_s debe ser > 0")
        if not 0.05 <= self.watchdog_s <= 5.0:
            raise ValueError("motors.watchdog_s debe estar entre 0.05 y 5 s")
        if not 0.0 <= self.deadzone < 0.5:
            raise ValueError("motors.deadzone debe estar en [0, 0.5)")


@dataclass(frozen=True)
class SafetyCfg:
    front_stop_cm: float = 20.0   # ToF frontal más cerca que esto: se bloquea el avance
    front_slow_cm: float = 35.0   # entre stop y slow la velocidad baja linealmente
    rear_stop_cm: float = 20.0    # ToF trasero: se bloquea la reversa
    sensor_timeout_s: float = 0.5  # si dejan de llegar datos de obstáculos, se bloquea el avance y la reversa

    def __post_init__(self) -> None:
        if not 0 < self.front_stop_cm < self.front_slow_cm:
            raise ValueError("safety: se requiere 0 < front_stop_cm < front_slow_cm")
        if self.rear_stop_cm <= 0:
            raise ValueError("safety.rear_stop_cm debe ser > 0")
        if not 0.1 <= self.sensor_timeout_s <= 5:
            raise ValueError("safety.sensor_timeout_s debe estar entre 0.1 y 5 s")


# Orden = ToF-1..ToF-4 de PINOUT.md §4.4 (el índice elige el XSHUT en Pins.tof_xshut y la dirección I2C)
TOF_NAMES = ("front", "front_left", "front_right", "rear")
TOF_FRONT = ("front", "front_left", "front_right")


@dataclass(frozen=True)
class TofCfg:
    enabled: bool = False       # SEGURO POR DEFECTO: sin sensores cableados, activarlo bloquearía el avance (fail-safe)
    sensors: tuple[str, ...] = TOF_NAMES   # los instalados: cada uno listado es obligatorio (si falla, bloquea)
    i2c_bus: int = 1
    gpiochip: int = 0
    base_address: int = 0x30    # ToF-1 = 0x30, ToF-2 = 0x31, ... (de fábrica todos nacen en 0x29)
    poll_hz: float = 20.0
    max_cm: float = 120.0       # más lejos que esto (o sin eco) se considera "libre"; el VL53L0X da ~120 cm útiles
    stale_s: float = 0.5        # sin lectura nueva durante este tiempo, el sensor cuenta como fallido

    def __post_init__(self) -> None:
        if not self.sensors or len(set(self.sensors)) != len(self.sensors):
            raise ValueError("tof.sensors debe tener al menos un sensor y sin repetir")
        bad = set(self.sensors) - set(TOF_NAMES)
        if bad:
            raise ValueError(f"tof.sensors: nombres desconocidos {sorted(bad)} (válidos: {list(TOF_NAMES)})")
        last = self.base_address + len(TOF_NAMES) - 1
        if not (0x08 <= self.base_address and last <= 0x77) or self.base_address <= 0x29 <= last:
            raise ValueError("tof.base_address debe dejar 4 direcciones libres en 0x08-0x77 sin incluir 0x29")
        if not 1 <= self.poll_hz <= 50:
            raise ValueError("tof.poll_hz debe estar entre 1 y 50")
        if not 20 <= self.max_cm <= 200:
            raise ValueError("tof.max_cm debe estar entre 20 y 200")
        if not 0.1 <= self.stale_s <= 5:
            raise ValueError("tof.stale_s debe estar entre 0.1 y 5 s")


@dataclass(frozen=True)
class CameraCfg:
    enabled: bool = True
    command: str = "rpicam-vid"   # de rpicam-apps; entrega MJPEG por su salida estándar
    width: int = 640
    height: int = 480
    fps: int = 15
    quality: int = 70             # calidad JPEG 1-100
    hflip: bool = False           # si la imagen sale espejada o al revés según cómo esté montada la cámara
    vflip: bool = False
    idle_s: float = 5.0           # sin espectadores este tiempo, se apaga la cámara (ahorra CPU y calor)

    def __post_init__(self) -> None:
        if not (160 <= self.width <= 1920 and 120 <= self.height <= 1080):
            raise ValueError("camera.width/height fuera de rango (160x120 a 1920x1080)")
        if not 1 <= self.fps <= 30:
            raise ValueError("camera.fps debe estar entre 1 y 30")
        if not 10 <= self.quality <= 95:
            raise ValueError("camera.quality debe estar entre 10 y 95")
        if not 0 <= self.idle_s <= 300:
            raise ValueError("camera.idle_s debe estar entre 0 y 300 s")


VISION_KINDS = ("person", "cat", "dog")


@dataclass(frozen=True)
class VisionCfg:
    enabled: bool = True              # necesita los modelos (scripts/get_models.py); si faltan, la visión queda apagada y lo dice
    models_dir: str = "models"        # relativos a la raíz del repo
    data_dir: str = "data/vision"     # identidades enseñadas (fotos y huellas); se queda en el robot, ignorado por git
    fps: float = 4.0                  # análisis por segundo (se reduce solo si la Pi se calienta)
    threads: int = 2                  # hilos del detector; el resto de la Pi sigue libre para motores y web
    kinds: tuple[str, ...] = VISION_KINDS
    detect_score: float = 0.5         # confianza mínima del detector
    cat_threshold: float = 0.65       # similitud mínima para decir "este es Pila"
    person_threshold: float = 0.40    # ídem para caras (SFace recomienda 0.363)
    margin: float = 0.05              # ventaja mínima sobre la 2.ª identidad cuando hay varias del mismo tipo
    follow_gain: float = 1.3          # ganancia de la mirada: >1 llega al borde antes de que el gato salga de cuadro
    gaze_mirror: bool = True          # ojos "hacia" el gato como los vería quien mira la pantalla de frente (ver README)
    lost_s: float = 1.5               # tiempo sin ver al gato antes de que los ojos vuelvan al centro
    teach_samples: int = 12           # muestras que se toman al enseñar una identidad
    teach_timeout_s: float = 45.0

    def __post_init__(self) -> None:
        bad = set(self.kinds) - set(VISION_KINDS)
        if bad or not self.kinds:
            raise ValueError(f"vision.kinds: valores no válidos {sorted(bad)} (válidos: {list(VISION_KINDS)})")
        if not 0.2 <= self.fps <= 15:
            raise ValueError("vision.fps debe estar entre 0.2 y 15")
        if not 1 <= self.threads <= 4:
            raise ValueError("vision.threads debe estar entre 1 y 4")
        if not 0.2 <= self.detect_score <= 0.95:
            raise ValueError("vision.detect_score debe estar entre 0.2 y 0.95")
        for name in ("cat_threshold", "person_threshold"):
            if not 0.05 <= getattr(self, name) <= 0.99:
                raise ValueError(f"vision.{name} debe estar entre 0.05 y 0.99")
        if not 0.0 <= self.margin <= 0.5:
            raise ValueError("vision.margin debe estar entre 0 y 0.5")
        if not 0.5 <= self.follow_gain <= 3:
            raise ValueError("vision.follow_gain debe estar entre 0.5 y 3")
        if not 0.2 <= self.lost_s <= 30:
            raise ValueError("vision.lost_s debe estar entre 0.2 y 30 s")
        if not 3 <= self.teach_samples <= 40:
            raise ValueError("vision.teach_samples debe estar entre 3 y 40")
        if not 5 <= self.teach_timeout_s <= 300:
            raise ValueError("vision.teach_timeout_s debe estar entre 5 y 300 s")


@dataclass(frozen=True)
class Config:
    sim: bool = False        # también se activa con ROBOK_SIM=1
    pins: Pins = field(default_factory=Pins)
    display: DisplayCfg = field(default_factory=DisplayCfg)
    voice: VoiceCfg = field(default_factory=VoiceCfg)
    web: WebCfg = field(default_factory=WebCfg)
    motors: MotorsCfg = field(default_factory=MotorsCfg)
    safety: SafetyCfg = field(default_factory=SafetyCfg)
    tof: TofCfg = field(default_factory=TofCfg)
    camera: CameraCfg = field(default_factory=CameraCfg)
    vision: VisionCfg = field(default_factory=VisionCfg)


def _build(cls: type, data: dict[str, Any], path: str = ""):
    """Construye un dataclass desde un dict, rechazando claves desconocidas (evita typos)."""
    known = {f.name: f for f in fields(cls)}
    unknown = set(data) - set(known)
    if unknown:
        raise ValueError(f"claves desconocidas en [{path or 'raíz'}]: {sorted(unknown)}")
    kwargs: dict[str, Any] = {}
    for name, value in data.items():
        default = getattr(cls(), name)
        if is_dataclass(default):
            kwargs[name] = _build(type(default), value, f"{path}.{name}" if path else name)
        elif isinstance(default, tuple):
            kwargs[name] = tuple(value)
        else:
            kwargs[name] = value
    return cls(**kwargs)


def _env_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def load_config(path: str | os.PathLike | None = None) -> Config:
    """Carga la configuración: `path`, o $ROBOK_CONFIG, o config.toml en la raíz del repo si existe."""
    chosen = Path(path or os.environ.get("ROBOK_CONFIG") or DEFAULT_CONFIG)
    data: dict[str, Any] = {}
    if chosen.exists():
        with chosen.open("rb") as f:
            data = tomllib.load(f)
    elif path or os.environ.get("ROBOK_CONFIG"):
        raise FileNotFoundError(f"no existe el archivo de configuración: {chosen}")
    cfg = _build(Config, data)
    if _env_true("ROBOK_SIM") and not cfg.sim:
        cfg = replace(cfg, sim=True)
    return cfg
