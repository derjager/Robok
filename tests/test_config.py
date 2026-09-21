import pytest

from robok.config import Config, Pins, load_config


def test_pines_no_se_repiten():
    used = Pins().all_used()
    assert len(used) == len(set(used)), "un GPIO está asignado dos veces"


def test_pines_coinciden_con_pinout():
    """Si esto falla, se cambió un pin sin actualizar PINOUT.md y docs/make_wiring_svg.py."""
    p = Pins()
    assert len(p.all_used()) == 20
    assert (p.motor_pwma, p.motor_ain1, p.motor_ain2, p.motor_pwmb) == (16, 19, 20, 21)
    assert (p.motor_bin1, p.motor_bin2, p.motor_stby) == (23, 24, 26)
    assert (p.servo1, p.servo2) == (12, 13)
    assert p.tof_xshut == (6, 5, 25, 4)
    assert (p.i2c_sda, p.i2c_scl) == (2, 3)
    assert (p.lcd_rst, p.lcd_dc, p.lcd_cs) == (27, 22, 8)


def test_config_por_defecto():
    cfg = Config()
    assert cfg.voice.voice == "es-419" and cfg.web.port == 8000 and cfg.sim is False


def test_toml_sobrescribe_valores(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text('sim = true\n[web]\nport = 9000\n[pins]\ntof_xshut = [1, 2, 3, 4]\n')
    cfg = load_config(f)
    assert cfg.sim and cfg.web.port == 9000 and cfg.web.host == "0.0.0.0"
    assert cfg.pins.tof_xshut == (1, 2, 3, 4)


def test_clave_desconocida_falla(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text("[web]\nprot = 9000\n")   # typo a propósito
    with pytest.raises(ValueError, match="prot"):
        load_config(f)


def test_archivo_explicito_inexistente_falla(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "no-existe.toml")


def test_env_activa_simulacion(monkeypatch, tmp_path):
    f = tmp_path / "c.toml"
    f.write_text("sim = false\n")
    monkeypatch.setenv("ROBOK_SIM", "1")
    assert load_config(f).sim is True


def test_config_del_repo_es_valida(monkeypatch):
    monkeypatch.delenv("ROBOK_CONFIG", raising=False)
    cfg = load_config()
    assert cfg.display.fps > 0


# --- motores y seguridad ---------------------------------------------------------------------
from robok.config import MotorsCfg, SafetyCfg  # noqa: E402


def test_motores_desactivados_por_defecto_y_con_tope_prudente():
    m = MotorsCfg()
    assert m.enabled is False and m.max_duty == 0.4


@pytest.mark.parametrize("kw", [{"max_duty": 0}, {"max_duty": 1.5}, {"pwm_hz": 10}, {"pwm_hz": 99999},
                                {"ramp_per_s": 0}, {"watchdog_s": 0.01}, {"watchdog_s": 60},
                                {"deadzone": 0.7}, {"backend": "pigpio"}])
def test_motors_rechaza_valores_peligrosos(kw):
    with pytest.raises(ValueError):
        MotorsCfg(**kw)


@pytest.mark.parametrize("kw", [{"front_stop_cm": 40}, {"front_stop_cm": 0}, {"rear_stop_cm": 0}])
def test_safety_rechaza_distancias_incoherentes(kw):
    with pytest.raises(ValueError):
        SafetyCfg(**kw)


def test_toml_de_motores_se_valida(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text("[motors]\nenabled = true\nmax_duty = 0.25\n[safety]\nfront_stop_cm = 25\n")
    cfg = load_config(f)
    assert cfg.motors.enabled and cfg.motors.max_duty == 0.25 and cfg.safety.front_stop_cm == 25
    f.write_text("[motors]\nmax_duty = 3\n")
    with pytest.raises(ValueError, match="max_duty"):
        load_config(f)


# --- ToF y cámara --------------------------------------------------------------------------------
from robok.config import CameraCfg, SafetyCfg, TofCfg  # noqa: E402


def test_tof_y_camara_por_defecto():
    cfg = Config()
    assert cfg.tof.enabled is False, "sin sensores cableados, activarlos bloquearía el avance"
    assert cfg.tof.sensors == ("front", "front_left", "front_right", "rear") and cfg.tof.base_address == 0x30
    assert cfg.camera.enabled and (cfg.camera.width, cfg.camera.height, cfg.camera.fps) == (640, 480, 15)


def test_toml_de_tof_y_camara(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text('[tof]\nenabled = true\nsensors = ["front", "rear"]\nmax_cm = 100\n'
                 '[camera]\nhflip = true\nfps = 10\n[safety]\nsensor_timeout_s = 1.0\n')
    cfg = load_config(f)
    assert cfg.tof.enabled and cfg.tof.sensors == ("front", "rear") and cfg.tof.max_cm == 100
    assert cfg.camera.hflip and cfg.camera.fps == 10 and cfg.safety.sensor_timeout_s == 1.0


@pytest.mark.parametrize("kw", [
    {"sensors": ()}, {"sensors": ("front", "front")}, {"sensors": ("front", "arriba")},
    {"base_address": 0x27}, {"base_address": 0x29}, {"base_address": 0x76}, {"base_address": 0x05},
    {"poll_hz": 0}, {"poll_hz": 500}, {"max_cm": 5}, {"stale_s": 0},
])
def test_tof_invalido(kw):
    with pytest.raises(ValueError):
        TofCfg(**kw)


@pytest.mark.parametrize("kw", [
    {"width": 10}, {"height": 5000}, {"fps": 0}, {"fps": 60}, {"quality": 5}, {"quality": 100}, {"idle_s": -1},
])
def test_camara_invalida(kw):
    with pytest.raises(ValueError):
        CameraCfg(**kw)


def test_sensor_timeout_invalido():
    with pytest.raises(ValueError):
        SafetyCfg(sensor_timeout_s=0.01)


def test_los_xshut_coinciden_con_los_nombres_de_los_sensores():
    from robok.config import TOF_NAMES
    assert len(TOF_NAMES) == len(Pins().tof_xshut) == 4


# --- visión ---------------------------------------------------------------------------------------
from robok.config import VisionCfg  # noqa: E402


def test_vision_por_defecto_y_toml(tmp_path):
    v = Config().vision
    assert v.enabled and v.fps == 4 and v.kinds == ("person", "cat", "dog") and v.gaze_mirror is True
    f = tmp_path / "c.toml"
    f.write_text('[vision]\nfps = 2\nkinds = ["cat"]\ncat_threshold = 0.7\ngaze_mirror = false\n')
    c = load_config(f).vision
    assert (c.fps, c.kinds, c.cat_threshold, c.gaze_mirror) == (2, ("cat",), 0.7, False)


@pytest.mark.parametrize("kw", [
    {"kinds": ()}, {"kinds": ("cat", "unicornio")}, {"fps": 0}, {"fps": 100}, {"threads": 0}, {"threads": 9},
    {"detect_score": 0.05}, {"detect_score": 0.99}, {"cat_threshold": 0}, {"person_threshold": 1.5}, {"margin": -1},
    {"follow_gain": 0}, {"lost_s": 0}, {"teach_samples": 1}, {"teach_samples": 500}, {"teach_timeout_s": 1},
])
def test_vision_invalida(kw):
    with pytest.raises(ValueError):
        VisionCfg(**kw)
