#!/usr/bin/env python3
"""Prueba de la pantalla 3.2" SPI (ILI9341, fb_ili9340) escribiendo directo al framebuffer.

Sin dependencias externas. Uso: python3 lcd_test.py [--hold]
  --hold  deja la ultima imagen (la cara de Wally) en pantalla al terminar.
"""
import glob
import sys
import time

FB_NAME = "fb_ili9340"

FONT = {
    "W": ["10001", "10001", "10001", "10101", "10101", "11011", "10001"],
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    "Y": ["10001", "10001", "01010", "00100", "00100", "00100", "00100"],
    "C": ["01110", "10001", "10000", "10000", "10000", "10001", "01110"],
    "D": ["11110", "10001", "10001", "10001", "10001", "10001", "11110"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "K": ["10001", "10010", "10100", "11000", "10100", "10010", "10001"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "I": ["01110", "00100", "00100", "00100", "00100", "00100", "01110"],
    "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "X": ["10001", "10001", "01010", "00100", "01010", "10001", "10001"],
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
    " ": ["00000"] * 7,
}

BLACK, WHITE = (0, 0, 0), (255, 255, 255)
RED, GREEN, BLUE = (255, 0, 0), (0, 255, 0), (0, 0, 255)
YELLOW, CYAN, MAGENTA = (255, 255, 0), (0, 255, 255), (255, 0, 255)


def find_fb():
    for path in glob.glob("/sys/class/graphics/fb*"):
        with open(f"{path}/name") as f:
            if f.read().strip() == FB_NAME:
                w, h = map(int, open(f"{path}/virtual_size").read().split(","))
                bpp = int(open(f"{path}/bits_per_pixel").read())
                return "/dev/" + path.rsplit("/", 1)[1], w, h, bpp
    sys.exit(f"No se encontro el framebuffer '{FB_NAME}'. Esta cargado el overlay tft9341?")


class Canvas:
    def __init__(self, w, h):
        self.w, self.h = w, h
        self.px = [BLACK] * (w * h)

    def fill(self, color):
        self.px = [color] * (self.w * self.h)

    def rect(self, x, y, w, h, color):
        for yy in range(max(0, y), min(self.h, y + h)):
            row = yy * self.w
            for xx in range(max(0, x), min(self.w, x + w)):
                self.px[row + xx] = color

    def circle(self, cx, cy, r, color):
        for yy in range(max(0, cy - r), min(self.h, cy + r + 1)):
            for xx in range(max(0, cx - r), min(self.w, cx + r + 1)):
                if (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r:
                    self.px[yy * self.w + xx] = color

    def line(self, x0, y0, x1, y1, color):
        steps = max(abs(x1 - x0), abs(y1 - y0), 1)
        for i in range(steps + 1):
            x = x0 + (x1 - x0) * i // steps
            y = y0 + (y1 - y0) * i // steps
            if 0 <= x < self.w and 0 <= y < self.h:
                self.px[y * self.w + x] = color

    def text(self, x, y, s, color, scale=1):
        for ch in s:
            for ry, row in enumerate(FONT[ch]):
                for rx, bit in enumerate(row):
                    if bit == "1":
                        self.rect(x + rx * scale, y + ry * scale, scale, scale, color)
            x += 6 * scale

    def to_rgb565(self):
        out = bytearray(self.w * self.h * 2)
        i = 0
        for r, g, b in self.px:
            v = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            out[i] = v & 0xFF  # little endian, como espera fbtft
            out[i + 1] = v >> 8
            i += 2
        return bytes(out)


def show(fb_path, canvas):
    with open(fb_path, "r+b", buffering=0) as fb:
        fb.write(canvas.to_rgb565())


def test_pattern(w, h):
    c = Canvas(w, h)
    # barras de color SMPTE simplificadas en la mitad superior
    colors = [WHITE, YELLOW, CYAN, GREEN, MAGENTA, RED, BLUE, BLACK]
    bw = w // len(colors)
    for i, col in enumerate(colors):
        c.rect(i * bw, 0, bw, h // 2, col)
    # degradado de grises en la parte inferior
    for x in range(w):
        g = x * 255 // (w - 1)
        c.rect(x, h // 2, 1, h // 6, (g, g, g))
    # borde blanco de 2 px: si se corta algun lado, hay overscan/rotacion mal
    c.rect(0, 0, w, 2, WHITE); c.rect(0, h - 2, w, 2, WHITE)
    c.rect(0, 0, 2, h, WHITE); c.rect(w - 2, 0, 2, h, WHITE)
    # marcadores de esquina para comprobar orientacion: TL rojo, TR verde, BL azul, BR blanco
    s = 20
    c.rect(2, 2, s, s, RED); c.rect(w - 2 - s, 2, s, s, GREEN)
    c.rect(2, h - 2 - s, s, s, BLUE); c.rect(w - 2 - s, h - 2 - s, s, s, WHITE)
    # diagonales y texto (el texto debe leerse al derecho, no en espejo)
    c.line(0, 0, w - 1, h - 1, MAGENTA); c.line(w - 1, 0, 0, h - 1, CYAN)
    c.rect(w // 2 - 78, h * 3 // 4 - 8, 156, 46, BLACK)
    c.text(w // 2 - 72, h * 3 // 4 - 3, "WALLY", YELLOW, 5)
    return c


def wally_face(w, h, blink=False):
    c = Canvas(w, h)
    c.fill(BLACK)
    for cx in (w // 2 - 60, w // 2 + 60):
        cy = h // 2 - 20
        if blink:
            c.rect(cx - 32, cy - 3, 64, 6, CYAN)
        else:
            c.circle(cx, cy, 34, CYAN)
            c.circle(cx + 6, cy + 4, 14, BLACK)  # pupila
    # sonrisa
    for x in range(-50, 51):
        y = int(x * x / 90)
        c.rect(w // 2 + x - 2, h // 2 + 55 + y - 20, 5, 5, CYAN)
    return c


def main():
    fb_path, w, h, bpp = find_fb()
    print(f"Framebuffer: {fb_path}  {w}x{h}  {bpp} bpp")
    if bpp != 16:
        sys.exit("Se esperaba RGB565 (16 bpp).")

    for name, col in [("ROJO", RED), ("VERDE", GREEN), ("AZUL", BLUE), ("BLANCO", WHITE), ("NEGRO", BLACK)]:
        print(f"  color solido: {name}")
        c = Canvas(w, h)
        c.fill(col)
        show(fb_path, c)
        time.sleep(1)

    print("  carta de ajuste (colores, gris, esquinas, texto)")
    show(fb_path, test_pattern(w, h))
    time.sleep(4)

    print("  cara de Wally")
    show(fb_path, wally_face(w, h))
    if "--hold" in sys.argv:
        return
    for _ in range(3):
        time.sleep(1)
        show(fb_path, wally_face(w, h, blink=True))
        time.sleep(0.15)
        show(fb_path, wally_face(w, h))
    time.sleep(1)
    show(fb_path, Canvas(w, h))  # deja la pantalla en negro


if __name__ == "__main__":
    main()
