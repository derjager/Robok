"""Voz de Wally: habla con espeak-ng y efectos de sonido, por la salida de audio por defecto (ALSA).

`say()` y `sound()` no bloquean: encolan y un hilo reproduce. Si la cola está llena se descarta
(mejor perder una frase que acumular retraso). El texto se pasa por stdin, nunca como argumento.
"""
from __future__ import annotations

import io
import logging
import math
import queue
import re
import shutil
import struct
import subprocess
import threading
import wave
from typing import Protocol

log = logging.getLogger(__name__)

# (velocidad en palabras/min, tono 0-99). Probados con es-419.
MOODS: dict[str, tuple[int, int]] = {
    "normal": (150, 50),
    "robot": (130, 20),
    "alegre": (165, 80),
    "triste": (120, 30),
    "susurro": (120, 40),
}

# efectos: lista de (frecuencia Hz, duración ms); 0 Hz = silencio
SOUNDS: dict[str, list[tuple[int, int]]] = {
    "ok": [(660, 90), (880, 140)],
    "error": [(300, 180), (220, 260)],
    "encontrado": [(523, 80), (659, 80), (784, 80), (1047, 180)],
    "encendido": [(392, 90), (523, 90), (659, 90), (784, 200)],
    "duda": [(500, 110), (0, 40), (420, 160)],
}

_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean_text(text: str, max_chars: int) -> str:
    """Quita caracteres de control, colapsa espacios y recorta."""
    text = _CTRL.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()[:max_chars]


def tone_wav(steps: list[tuple[int, int]], rate: int = 22050, volume: float = 0.5) -> bytes:
    """Genera un WAV mono 16 bit con la secuencia de tonos (con rampas para evitar clics)."""
    frames = bytearray()
    for freq, ms in steps:
        n = int(rate * ms / 1000)
        ramp = max(1, int(rate * 0.008))
        for i in range(n):
            env = min(1.0, i / ramp, (n - i) / ramp)
            v = 0.0 if freq <= 0 else math.sin(2 * math.pi * freq * i / rate)
            frames += struct.pack("<h", int(volume * 32767 * env * v))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return buf.getvalue()


class Runner(Protocol):
    def speak(self, text: str, voice: str, speed: int, pitch: int) -> None: ...
    def play_wav(self, data: bytes) -> None: ...
    def stop(self) -> None: ...


class EspeakRunner:
    """Reproduce con `espeak-ng | aplay` (procesos hijos, sin shell)."""

    def __init__(self) -> None:
        self._procs: list[subprocess.Popen] = []
        self._lock = threading.Lock()

    @staticmethod
    def available() -> bool:
        return shutil.which("espeak-ng") is not None and shutil.which("aplay") is not None

    def _run(self, first: list[str], stdin_data: bytes) -> None:
        p1 = subprocess.Popen(first, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        p2 = subprocess.Popen(["aplay", "-q", "-"], stdin=p1.stdout, stderr=subprocess.DEVNULL)
        p1.stdout.close()
        with self._lock:
            self._procs = [p1, p2]
        try:
            p1.stdin.write(stdin_data)
            p1.stdin.close()
        except BrokenPipeError:
            pass
        p2.wait()
        p1.wait()

    def speak(self, text: str, voice: str, speed: int, pitch: int) -> None:
        self._run(["espeak-ng", "-v", voice, "-s", str(speed), "-p", str(pitch), "--stdout"], text.encode())

    def play_wav(self, data: bytes) -> None:
        p = subprocess.Popen(["aplay", "-q", "-"], stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        with self._lock:
            self._procs = [p]
        try:
            p.communicate(data)
        except BrokenPipeError:
            pass

    def stop(self) -> None:
        with self._lock:
            procs, self._procs = self._procs, []
        for p in procs:
            if p.poll() is None:
                p.terminate()


class FakeRunner:
    """Registra lo que se diría, sin sonido. Para pruebas y modo simulado."""

    def __init__(self) -> None:
        self.spoken: list[tuple[str, str, int, int]] = []
        self.played: list[int] = []

    def speak(self, text: str, voice: str, speed: int, pitch: int) -> None:
        self.spoken.append((text, voice, speed, pitch))

    def play_wav(self, data: bytes) -> None:
        self.played.append(len(data))

    def stop(self) -> None:
        pass


class Voice:
    def __init__(self, runner: Runner, voice: str = "es-419", max_chars: int = 200, enabled: bool = True,
                 queue_size: int = 4):
        self.runner = runner
        self.voice = voice
        self.max_chars = max_chars
        self.enabled = enabled
        self._q: queue.Queue = queue.Queue(maxsize=queue_size)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wavs = {name: tone_wav(steps) for name, steps in SOUNDS.items()}

    def say(self, text: str, mood: str = "normal") -> bool:
        """Encola una frase. Devuelve False si no se pudo (vacía, desactivada o cola llena)."""
        text = clean_text(text, self.max_chars)
        if not self.enabled or not text:
            return False
        speed, pitch = MOODS.get(mood, MOODS["normal"])
        return self._put(("say", text, speed, pitch))

    def sound(self, name: str) -> bool:
        if not self.enabled or name not in self._wavs:
            return False
        return self._put(("wav", name))

    def _put(self, item: tuple) -> bool:
        try:
            self._q.put_nowait(item)
            return True
        except queue.Full:
            log.warning("cola de voz llena; se descarta %s", item[0])
            return False

    def stop(self) -> None:
        """Silencia lo que suena y vacía la cola."""
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass
        self.runner.stop()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="voice", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self.stop()
        self._q.put(None)
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.is_set():
            item = self._q.get()
            if item is None:
                break
            try:
                if item[0] == "say":
                    self.runner.speak(item[1], self.voice, item[2], item[3])
                else:
                    self.runner.play_wav(self._wavs[item[1]])
            except Exception:
                log.exception("error reproduciendo audio")
