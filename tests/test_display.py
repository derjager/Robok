import pytest
from PIL import Image

from robok.hal.display import DisplayNotFound, FakeDisplay, find_framebuffer, rgb565_bytes


def px(color):
    return rgb565_bytes(Image.new("RGB", (1, 1), color))


@pytest.mark.parametrize("color,value", [
    ((255, 0, 0), 0xF800), ((0, 255, 0), 0x07E0), ((0, 0, 255), 0x001F),
    ((255, 255, 255), 0xFFFF), ((0, 0, 0), 0x0000),
])
def test_rgb565_colores_puros(color, value):
    assert px(color) == value.to_bytes(2, "little")


def test_rgb565_tamano():
    assert len(rgb565_bytes(Image.new("RGB", (240, 320)))) == 240 * 320 * 2


def _fake_sysfs(tmp_path, fbs):
    for name, drv, size, bpp in fbs:
        d = tmp_path / name
        d.mkdir()
        (d / "name").write_text(drv + "\n")
        (d / "virtual_size").write_text(f"{size[0]},{size[1]}\n")
        (d / "bits_per_pixel").write_text(f"{bpp}\n")
    return tmp_path


def test_encuentra_framebuffer_por_nombre_y_no_por_numero(tmp_path):
    sysfs = _fake_sysfs(tmp_path, [("fb0", "BCM2708 FB", (480, 360), 32),
                                   ("fb1", "fb_ili9340", (240, 320), 16)])
    assert find_framebuffer("fb_ili9340", sysfs) == ("/dev/fb1", (240, 320))


def test_framebuffer_ausente(tmp_path):
    sysfs = _fake_sysfs(tmp_path, [("fb0", "BCM2708 FB", (480, 360), 32)])
    with pytest.raises(DisplayNotFound):
        find_framebuffer("fb_ili9340", sysfs)


def test_framebuffer_con_bpp_inesperado(tmp_path):
    sysfs = _fake_sysfs(tmp_path, [("fb1", "fb_ili9340", (240, 320), 32)])
    with pytest.raises(DisplayNotFound, match="16 bpp"):
        find_framebuffer("fb_ili9340", sysfs)


def test_fake_display_guarda_el_ultimo_cuadro():
    d = FakeDisplay()
    assert d.size == (240, 320) and d.last is None
    d.show(Image.new("RGB", d.size, (1, 2, 3)))
    assert d.frames == 1 and d.last.getpixel((0, 0)) == (1, 2, 3)
