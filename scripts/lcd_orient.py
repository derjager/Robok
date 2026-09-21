#!/usr/bin/env python3
"""Pantalla de orientacion para el LCD: sirve para elegir el valor de `rotate`.

Dibuja, en coordenadas del framebuffer: una flecha hacia ARRIBA, el texto ARRIBA,
una I en el borde izquierdo y una D en el derecho, cuadros de color en las esquinas
(arriba-izq rojo, arriba-der verde, abajo-izq azul, abajo-der blanco) y el tamano
del framebuffer. Con la rotacion correcta la flecha apunta hacia arriba, todo se lee
al derecho (no en espejo) y los colores caen en las esquinas indicadas.
"""
from lcd_test import BLACK, BLUE, CYAN, GREEN, RED, WHITE, YELLOW, Canvas, find_fb, show


def main():
    fb_path, w, h, bpp = find_fb()
    print(f"Framebuffer: {fb_path}  {w}x{h}  {'apaisado' if w > h else 'vertical'}")
    c = Canvas(w, h)
    c.fill(BLACK)
    cx = w // 2

    # marco y esquinas
    c.rect(0, 0, w, 1, WHITE); c.rect(0, h - 1, w, 1, WHITE)
    c.rect(0, 0, 1, h, WHITE); c.rect(w - 1, 0, 1, h, WHITE)
    s = 18
    c.rect(1, 1, s, s, RED); c.rect(w - 1 - s, 1, s, s, GREEN)
    c.rect(1, h - 1 - s, s, s, BLUE); c.rect(w - 1 - s, h - 1 - s, s, s, WHITE)

    # texto y flecha
    c.text(cx - 3 * 6 * 3, 6, "ARRIBA", WHITE, 3)
    head_h = int(min(w, h) * 0.30)
    head_w = int(head_h * 1.5)
    top = 36
    for row in range(head_h):
        half = (row * head_w // 2) // head_h
        c.rect(cx - half, top + row, 2 * half + 1, 1, YELLOW)
    shaft_w = head_w // 3
    c.rect(cx - shaft_w // 2, top + head_h, shaft_w, int(h * 0.22), YELLOW)

    # izquierda / derecha
    c.text(8, h // 2 - 12, "I", CYAN, 4)
    c.text(w - 8 - 5 * 4, h // 2 - 12, "D", CYAN, 4)

    # tamano del framebuffer
    label = f"{w}X{h}"
    c.text(cx - len(label) * 6, h - 24, label, WHITE, 2)
    show(fb_path, c)


if __name__ == "__main__":
    main()
