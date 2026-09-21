"""Robot: agrupa los subsistemas y controla su ciclo de vida.

Hoy: display + cara + voz + motores + ToF (obstáculos) + cámara. Los servos y la visión vienen después.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from robok import __version__
from robok.config import Config
from robok.hal.camera import Camera, create_camera
from robok.hal.display import Display, FakeDisplay, create_display
from robok.hal.motors import FakeMotors, LgpioBackend, Motors, Tb6612Motors
from robok.hal.tof import Tof, create_tof
from robok.services.drive import Drive
from robok.services.face import Face
from robok.services.obstacles import Obstacles
from robok.services.vision import Vision
from robok.services.voice import EspeakRunner, FakeRunner, Runner, Voice
from robok.vision.gallery import Gallery
from robok.vision.models import load_backends, missing, resolve_dir

log = logging.getLogger(__name__)


def cpu_temp_c(path: str = "/sys/class/thermal/thermal_zone0/temp") -> float | None:
    try:
        return round(int(Path(path).read_text()) / 1000, 1)
    except (OSError, ValueError):
        return None


@dataclass
class Robot:
    cfg: Config
    display: Display
    display_desc: str
    face: Face
    voice: Voice
    voice_desc: str
    motors_desc: str
    drive: Drive
    camera: Camera | None = None
    tof: Tof | None = None
    obstacles: Obstacles | None = None
    vision: Vision | None = None
    started_at: float = field(default_factory=time.monotonic)
    _started: bool = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.started_at = time.monotonic()
        self.voice.start()
        self.face.start()
        if self.tof is not None and self.obstacles is not None:
            self.tof.start()             # tarda ~1 s: inicializa los sensores uno por uno
            self.obstacles.update()      # el primer dato antes de mover nada
            self.obstacles.start()
        self.drive.start()
        if self.vision is not None:
            self.vision.start()
        log.info("Wally listo (pantalla: %s, voz: %s, motores: %s, ToF: %s, cámara: %s)", self.display_desc,
                 self.voice_desc, self.motors_desc, self.tof.desc if self.tof else "no",
                 self.camera.desc if self.camera else "no")

    def stop(self) -> None:
        if self.vision is not None:
            self.vision.close()     # suelta la cámara y devuelve los ojos al centro
        if self.camera is not None:
            self.camera.close()     # se enciende sola con un espectador, aunque el robot no se haya arrancado
        if not self._started:
            return
        self._started = False
        self.drive.close()          # lo primero: los motores quedan parados y el driver liberado
        if self.obstacles is not None:
            self.obstacles.close()
        if self.tof is not None:
            self.tof.close()
        self.face.stop()
        self.voice.close()
        self.display.close()

    def status(self) -> dict:
        return {
            "name": "Wally",
            "version": __version__,
            "sim": self.cfg.sim,
            "uptime_s": round(time.monotonic() - self.started_at, 1),
            "cpu_temp_c": cpu_temp_c(),
            "display": self.display_desc,
            "voice": self.voice_desc,
            "motors": self.motors_desc,
            "face": self.face.state(),
            "drive": self.drive.status(),
            "tof": self.obstacles.status() if self.obstacles else None,
            "camera": self.camera.status() if self.camera else None,
            "vision": self.vision.status() if self.vision else None,
        }

    # --- reacciones a eventos de seguridad (la cara y la voz dan la cara por el robot) -------------
    def on_drive_event(self, kind: str) -> None:
        if kind == "estop":
            self.face.set_expression("angry", hold=3.0)
            self.voice.sound("error")
        elif kind == "estop_reset":
            self.face.set_expression("neutral")
            self.voice.sound("ok")
        elif kind in {"obstaculo_frente", "obstaculo_atras"}:
            self.face.set_expression("surprised", hold=1.5)
        elif kind == "sensor_fallo":
            self.face.set_expression("sad", hold=3.0)
            self.voice.sound("error")
        elif kind == "teach_done":
            self.face.set_expression("happy", hold=2.5)
            self.voice.sound("ok")
        elif kind == "teach_timeout":
            self.face.set_expression("thinking", hold=2.0)
            self.voice.sound("duda")
        elif kind == "watchdog":
            self.face.set_expression("thinking", hold=2.0)
            self.voice.sound("duda")


def build_motors(cfg: Config) -> tuple[Motors, str]:
    """Motores reales solo si motors.enabled = true y no es --sim; ante cualquier duda, simulados."""
    if cfg.sim:
        return FakeMotors(), "simulados (--sim)"
    if not cfg.motors.enabled:
        return FakeMotors(), "simulados (motors.enabled = false)"
    gpio = None
    try:
        gpio = LgpioBackend(cfg.motors.gpiochip)
        motors = Tb6612Motors(gpio, cfg.pins, cfg.motors)
        return motors, f"TB6612 (lgpio, {cfg.motors.pwm_hz} Hz, tope {cfg.motors.max_duty:.0%})"
    except Exception as e:
        log.error("no se pudieron activar los motores reales: %s", e)
        if gpio is not None:
            try:
                gpio.close()
            except Exception:
                log.exception("no se pudo liberar el GPIO tras el fallo")
        return FakeMotors(), f"ERROR ({e}); motores simulados"


def build_vision(cfg: Config, camera: Camera | None, face: Face) -> Vision | None:
    """La visión, con modelos reales (o simulados con --sim). Si faltan los modelos queda "no disponible" y lo dice."""
    v = cfg.vision
    if not v.enabled or camera is None:
        return None
    data_dir = resolve_dir(v.data_dir)
    if cfg.sim:
        from robok.vision.sim import SimDetector, SimEmbedder, SimFaces  # noqa: PLC0415

        data_dir = data_dir.with_name(data_dir.name + "-sim")          # huellas de mentira: no se mezclan con las reales
        backends = (SimDetector(), SimEmbedder(), SimFaces(), "simulada")
        unavailable = None
    else:
        backends, unavailable = (None, None, None, ""), None
        gone = missing(v)
        if any(m.key == "detector" for m in gone):
            unavailable = "faltan los modelos: ejecuta .venv/bin/python scripts/get_models.py"
        else:
            try:
                backends = load_backends(v)
            except Exception as e:
                log.exception("no se pudieron cargar los modelos de visión")
                unavailable = f"no se pudieron cargar los modelos ({type(e).__name__}: {e})"
    detector, embedder, faces, desc = backends
    return Vision(v, camera, Gallery(data_dir), detector, embedder, faces, face.look, unavailable=unavailable,
                  settings_path=data_dir / "settings.json", temp_fn=cpu_temp_c, desc=desc)


def build_robot(cfg: Config) -> Robot:
    display, display_desc = create_display(cfg.display.framebuffer_name, cfg.sim)
    face = Face(display, fps=cfg.display.fps)

    runner: Runner
    if cfg.sim:
        runner, voice_desc = FakeRunner(), "simulada"
    elif EspeakRunner.available():
        runner, voice_desc = EspeakRunner(), f"espeak-ng {cfg.voice.voice}"
    else:
        log.warning("espeak-ng o aplay no están instalados; la voz queda simulada")
        runner, voice_desc = FakeRunner(), "simulada (falta espeak-ng/aplay)"
    voice = Voice(runner, voice=cfg.voice.voice, max_chars=cfg.voice.max_chars, enabled=cfg.voice.enabled)
    motors, motors_desc = build_motors(cfg)
    drive = Drive(motors, cfg.motors, cfg.safety)
    tof = create_tof(cfg.tof, cfg.pins, cfg.sim)
    camera = create_camera(cfg.camera, cfg.sim, scene=cfg.sim and cfg.vision.enabled)
    robot = Robot(cfg=cfg, display=display, display_desc=display_desc, face=face, voice=voice,
                  voice_desc=voice_desc, motors_desc=motors_desc, drive=drive, camera=camera, tof=tof)
    robot.vision = build_vision(cfg, camera, face)
    if robot.vision is not None:
        robot.vision._on_event = robot.on_drive_event
    if tof is not None:
        drive.set_obstacles(0.0, 0.0)      # fail-safe: bloqueado hasta que lleguen lecturas reales de los sensores
        robot.obstacles = Obstacles(tof, drive, hz=cfg.tof.poll_hz, on_event=robot.on_drive_event,
                                   safety=cfg.safety)
    robot.drive._on_event = robot.on_drive_event
    return robot


__all__ = ["Robot", "build_robot", "cpu_temp_c", "FakeDisplay"]
