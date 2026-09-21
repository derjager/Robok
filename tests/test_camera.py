import io
import os
import stat
import sys
import textwrap
import time

import pytest
from PIL import Image

import robok.hal.camera as cam
from robok.config import CameraCfg
from robok.hal.camera import FakeCamera, MjpegSplitter, RpicamCamera, create_camera


def jpeg(w=64, h=48, quality=70, progressive=False, seed=0):
    img = Image.effect_noise((w, h), 60 + seed).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, progressive=progressive)
    return buf.getvalue()


def wait_for(cond, timeout=3.0, step=0.01):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


def split_all(data, chunk):
    s, out = MjpegSplitter(), []
    for i in range(0, len(data), chunk):
        out += list(s.feed(data[i:i + chunk]))
    return out


# --- separador de fotogramas ---------------------------------------------------------------------

@pytest.mark.parametrize("chunk", [1, 3, 100, 4096, 10**6])
def test_separa_jpeg_concatenados_sin_importar_el_tamano_de_lectura(chunk):
    frames = [jpeg(seed=i) for i in range(5)]
    got = split_all(b"".join(frames), chunk)
    assert got == frames
    for g in got:
        Image.open(io.BytesIO(g)).verify()


@pytest.mark.parametrize("quality", [1, 10, 50, 95])
def test_calidades_extremas(quality):
    frames = [jpeg(80, 60, quality, seed=i) for i in range(3)]
    assert split_all(b"".join(frames), 7) == frames


def test_jpeg_progresivo_con_varios_scan():
    frames = [jpeg(120, 90, 80, progressive=True, seed=i) for i in range(3)]
    assert b"\xff\xda" in frames[0]
    assert split_all(b"".join(frames), 50) == frames


def test_ffd9_dentro_de_una_tabla_no_corta_la_imagen():
    """Un FF D9 dentro de un segmento (p. ej. valores de cuantización) no es el fin de la imagen."""
    real = jpeg()
    trap = b"\xff\xdb\x00\x06\xff\xd9\xff\xd8"           # segmento DQT con datos "FF D9 FF D8"
    poisoned = real[:2] + trap + real[2:]
    assert split_all(poisoned + real, 13) == [poisoned, real]


def test_basura_antes_y_entre_fotogramas_se_descarta():
    a, b = jpeg(seed=1), jpeg(seed=2)
    data = b"\x00\x01garbage" + a + b"\xff\x00\x11" + b
    assert split_all(data, 5) == [a, b]


def test_fotograma_cortado_espera_el_resto_y_no_emite_a_medias():
    a = jpeg()
    s = MjpegSplitter()
    assert list(s.feed(a[:-5])) == []
    assert list(s.feed(a[-5:])) == [a]


def test_jpeg_corrupto_no_bloquea_al_siguiente():
    a = jpeg(seed=3)
    corrupt = b"\xff\xd8" + b"\x12\x34" * 10                # no tiene estructura de segmentos
    assert split_all(corrupt + a, 9) == [a]


def test_buffer_acotado_si_nunca_termina_un_fotograma():
    s = MjpegSplitter()
    s.feed(b"\xff\xd8\xff\xdb\xff\xff")                     # longitud de segmento enorme: nunca completa
    assert list(s.feed(b"\x00" * 1000)) == []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cam, "MAX_BUFFER", 500)
        s2 = MjpegSplitter()
        assert list(s2.feed(b"\xff\xd8\xff\xdb\xff\xff" + b"\x00" * 1000)) == []
        assert len(s2._buf) == 0


# --- ciclo de vida de la cámara (simulada) ------------------------------------------------------

@pytest.fixture
def fake():
    c = FakeCamera(CameraCfg(width=160, height=120, fps=30, idle_s=0.15))
    yield c
    c.close()


def test_apagada_hasta_que_alguien_mira(fake):
    time.sleep(0.1)
    assert fake.latest() is None and fake.status()["on"] is False
    fake.acquire()
    assert wait_for(lambda: fake.latest() is not None)
    assert wait_for(lambda: fake.status()["fps"] > 0)       # el fps se mide desde el segundo fotograma
    s = fake.status()
    assert s["on"] and s["streaming"] and s["viewers"] == 1
    Image.open(io.BytesIO(fake.latest()[1])).verify()


def test_se_apaga_sola_al_irse_el_ultimo_espectador(fake):
    fake.acquire()
    fake.acquire()
    assert wait_for(lambda: fake.latest() is not None)
    fake.release()
    time.sleep(0.4)
    assert fake.status()["on"], "queda un espectador: sigue encendida"
    fake.release()
    assert wait_for(lambda: not fake.status()["on"], timeout=2)
    n = fake.frames
    time.sleep(0.2)
    assert fake.frames == n, "apagada no genera fotogramas"


def test_volver_a_mirar_antes_del_apagado_cancela_el_apagado(fake):
    fake.acquire()
    fake.release()
    time.sleep(0.05)
    fake.acquire()
    time.sleep(0.4)
    assert fake.status()["on"] and fake.status()["viewers"] == 1


def test_se_puede_reencender_tras_apagarse(fake):
    fake.acquire(); fake.release()
    assert wait_for(lambda: not fake.status()["on"], timeout=2)
    fake.acquire()
    assert wait_for(lambda: fake.status()["streaming"])


def test_suscriptores_reciben_fotogramas_y_pueden_darse_de_baja(fake):
    got = []
    unsub = fake.subscribe(lambda seq, j: got.append(seq))
    fake.acquire()
    assert wait_for(lambda: len(got) >= 3)
    assert got == sorted(got) and len(set(got)) == len(got), "secuencia creciente"
    unsub()
    n = len(got)
    time.sleep(0.15)
    assert len(got) == n


def test_un_suscriptor_que_falla_se_da_de_baja_y_no_rompe_la_captura(fake):
    def bad(*_):
        raise RuntimeError("cliente roto")
    ok = []
    fake.subscribe(bad)
    fake.subscribe(lambda s, j: ok.append(s))
    fake.acquire()
    assert wait_for(lambda: len(ok) >= 3)
    assert bad not in fake._subs


def test_release_de_mas_no_da_negativos(fake):
    fake.release(); fake.release()
    assert fake.status()["viewers"] == 0


def test_cerrar_es_idempotente_y_luego_no_se_puede_usar(fake):
    fake.acquire()
    assert wait_for(lambda: fake.latest() is not None)
    fake.close(); fake.close()
    assert fake.status()["on"] is False
    with pytest.raises(RuntimeError):
        fake.acquire()


def test_create_camera_segun_la_situacion(monkeypatch):
    assert create_camera(CameraCfg(enabled=False), sim=False) is None
    assert "simulada (--sim)" in create_camera(CameraCfg(), sim=True).desc
    monkeypatch.setattr(RpicamCamera, "available", staticmethod(lambda c="": False))
    c = create_camera(CameraCfg(command="no-existe"), sim=False)
    assert isinstance(c, FakeCamera) and "falta no-existe" in c.desc
    monkeypatch.setattr(RpicamCamera, "available", staticmethod(lambda c="": True))
    assert isinstance(create_camera(CameraCfg(), sim=False), RpicamCamera)


# --- cámara real (con un rpicam-vid falso) ------------------------------------------------------

def stub(tmp_path, body, name="rpicam-stub"):
    """Ejecutable falso que hace de rpicam-vid: ignora los argumentos y ejecuta `body` (Python)."""
    path = tmp_path / name
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def frames_script(n_frames=None, delay=0.02, log=None):
    return f"""
import io, sys, time, os
from PIL import Image
{"open(%r, 'a').write('x')" % log if log else ""}
def frame(i):
    b = io.BytesIO(); Image.new('RGB', (32, 24), (i % 250, 10, 10)).save(b, 'JPEG'); return b.getvalue()
i = 0
while {"i < %d" % n_frames if n_frames else "True"}:
    sys.stdout.buffer.write(frame(i)); sys.stdout.buffer.flush(); i += 1; time.sleep({delay})
"""


def starts(log):
    return len(log.read_text()) if log.exists() else 0


def test_comando_de_rpicam_con_giros_y_calidad():
    c = RpicamCamera(CameraCfg(width=800, height=600, fps=10, quality=55, hflip=True, vflip=True))
    cmd = c.command()
    assert cmd[0] == "rpicam-vid" and "mjpeg" in cmd
    assert cmd[cmd.index("-o") + 1] == "-", "los JPEG salen por la salida estándar"
    for flag, val in (("--width", "800"), ("--height", "600"), ("--framerate", "10"), ("--quality", "55")):
        assert cmd[cmd.index(flag) + 1] == val
    assert "--hflip" in cmd and "--vflip" in cmd and "-n" in cmd and "--flush" in cmd
    cmd2 = RpicamCamera(CameraCfg()).command()
    assert "--hflip" not in cmd2 and "--vflip" not in cmd2


def test_lee_fotogramas_del_proceso_y_lo_mata_al_apagarse(tmp_path):
    c = RpicamCamera(CameraCfg(command=stub(tmp_path, frames_script()), idle_s=0.1))
    got = []
    c.subscribe(lambda s, j: got.append(j))
    c.acquire()
    try:
        assert wait_for(lambda: len(got) >= 5)
        for g in got[:5]:
            assert Image.open(io.BytesIO(g)).size == (32, 24)
        c.release()
        assert wait_for(lambda: not c.status()["on"], timeout=3)
        n = len(got)
        time.sleep(0.2)
        assert len(got) <= n + 1, "el proceso terminó: ya no llegan fotogramas"
    finally:
        c.close()


def test_si_el_proceso_muere_lo_reintenta_y_dice_por_que(tmp_path, monkeypatch):
    log = tmp_path / "starts"
    body = frames_script(n_frames=2, delay=0.01, log=str(log)) + "\nprint('camera busy', file=sys.stderr)\nsys.exit(3)\n"
    c = RpicamCamera(CameraCfg(command=stub(tmp_path, body), idle_s=0.1))
    monkeypatch.setattr(cam, "BACKOFF_S", (0.05, 0.1))
    c.acquire()
    try:
        assert wait_for(lambda: starts(log) >= 2, timeout=5), "se reinició tras morir"
        assert wait_for(lambda: c.status()["error"] is not None or c.latest() is not None)
    finally:
        c.close()


def test_comando_inexistente_no_tumba_el_hilo(tmp_path):
    c = RpicamCamera(CameraCfg(command=str(tmp_path / "no-existe")))
    c.acquire()
    try:
        assert wait_for(lambda: c.status()["error"] is not None)
        assert "no se pudo lanzar" in c.status()["error"] and c.status()["on"]
    finally:
        c.close()


def test_proceso_vivo_pero_sin_imagenes_se_reinicia(tmp_path, monkeypatch):
    log = tmp_path / "starts"
    body = f"import time\nopen({str(log)!r}, 'a').write('x')\ntime.sleep(30)\n"
    monkeypatch.setattr(cam, "STALL_S", 0.4)
    c = RpicamCamera(CameraCfg(command=stub(tmp_path, body)))
    c.acquire()
    try:
        assert wait_for(lambda: starts(log) >= 2, timeout=8), "un proceso mudo debe reiniciarse"
        assert "sin imágenes" in (c.status()["error"] or "")
    finally:
        c.close()


def test_fps_se_mide_en_una_ventana_reciente():
    c = FakeCamera(CameraCfg())
    now = time.monotonic()
    c._times.extend([now - 1.8 + i * 0.1 for i in range(19)])          # 10 fps durante 1,8 s
    assert c.fps() == pytest.approx(10.0, rel=0.05)
    c._times.clear()
    c._times.extend([now - 10, now - 9.9])                              # datos viejos no cuentan
    assert c.fps() == 0.0 and c.status()["fps"] == 0.0
