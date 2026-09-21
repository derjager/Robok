#!/usr/bin/env python3
"""Genera docs/wiring.svg: diagrama de conexion pin a pin de Wally.

Los pines salen de la tabla PINS (misma asignacion que PINOUT.md). Solo usa la
libreria estandar.  Uso:  python3 docs/make_wiring_svg.py
Tambien imprime la lista de cables (markdown) para pegarla en PINOUT.md.
"""
import os
import random
from xml.sax.saxutils import escape

W, H = 1820, 1540
FONT = "DejaVu Sans, Verdana, Arial, Helvetica, sans-serif"

NETS = {  # color, ancho, trazo, nombre para la leyenda
    "GND": ("#1b1b1b", 3.4, None, "GND (masa común)"),
    "V5":  ("#d62728", 3.4, None, "5 V (sale del BEC)"),
    "V3":  ("#f28e00", 3.4, None, "3.3 V (de la Pi)"),
    "BAT": ("#c2185b", 3.4, None, "BAT+ 7.4 V (LiPo 2S)"),
    "VM":  ("#8d5524", 3.4, None, "VM / motores (3 V)"),
    "SPI": ("#00838f", 1.9, None, "LCD: SPI + DC + RST"),
    "MOT": ("#1565c0", 1.9, None, "TB6612: PWM, IN1/IN2, STBY"),
    "SRV": ("#7b1fa2", 1.9, None, "Servos: señal PWM"),
    "SDA": ("#2e7d32", 1.9, None, "I2C SDA"),
    "SCL": ("#2e7d32", 1.9, "7 4", "I2C SCL"),
    "XSH": ("#546e7a", 1.9, None, "XSHUT de los ToF"),
}

# (pin, gpio, funcion, grupo).  grupo: gnd v5 v3 spi i2c mot srv xsh free rsv
PINS = [
    (1, "3V3", "3.3 V", "v3"), (2, "5V", "5 V > LCD", "v5"),
    (3, "GPIO2 SDA1", "I2C SDA", "i2c"), (4, "5V", "5 V < BEC 1", "v5"),
    (5, "GPIO3 SCL1", "I2C SCL", "i2c"), (6, "GND", "GND > LCD", "gnd"),
    (7, "GPIO4", "XSHUT ToF-4", "xsh"), (8, "GPIO14 TXD", "libre 1", "free"),
    (9, "GND", "GND < bus", "gnd"), (10, "GPIO15 RXD", "libre 1", "free"),
    (11, "GPIO17", "libre 2", "free"), (12, "GPIO18", "libre", "free"),
    (13, "GPIO27", "LCD RST", "spi"), (14, "GND", "GND", "gnd0"),
    (15, "GPIO22", "LCD DC", "spi"), (16, "GPIO23", "BIN1", "mot"),
    (17, "3V3", "3.3 V > LCD", "v3"), (18, "GPIO24", "BIN2", "mot"),
    (19, "GPIO10 MOSI", "LCD SI", "spi"), (20, "GND", "GND", "gnd0"),
    (21, "GPIO9 MISO", "libre 2", "free"), (22, "GPIO25", "XSHUT ToF-3", "xsh"),
    (23, "GPIO11 SCLK", "LCD SCK", "spi"), (24, "GPIO8 CE0", "LCD CS", "spi"),
    (25, "GND", "GND", "gnd0"), (26, "GPIO7 CE1", "libre 2", "free"),
    (27, "GPIO0", "reservado", "rsv"), (28, "GPIO1", "reservado", "rsv"),
    (29, "GPIO5", "XSHUT ToF-2", "xsh"), (30, "GND", "GND", "gnd0"),
    (31, "GPIO6", "XSHUT ToF-1", "xsh"), (32, "GPIO12", "Servo 1 PWM", "srv"),
    (33, "GPIO13", "Servo 2 PWM", "srv"), (34, "GND", "GND", "gnd0"),
    (35, "GPIO19", "AIN1", "mot"), (36, "GPIO16", "PWMA", "mot"),
    (37, "GPIO26", "STBY", "mot"), (38, "GPIO20", "AIN2", "mot"),
    (39, "GND", "GND", "gnd0"), (40, "GPIO21", "PWMB", "mot"),
]
GROUP_FILL = {"gnd": "#1b1b1b", "gnd0": "#9e9e9e", "v5": "#d62728", "v3": "#f28e00",
              "spi": "#00838f", "i2c": "#2e7d32", "mot": "#1565c0", "srv": "#7b1fa2",
              "xsh": "#546e7a", "free": "#c8e6c9", "rsv": "#e0e0e0"}
GROUP_TEXT_DARK = {"free", "rsv", "gnd0"}

# --- geometria -----------------------------------------------------------------
STRIP_X, STRIP_W, STRIP_TOP, RH = 470, 230, 250, 25
STRIP_R = STRIP_X + STRIP_W
DX, DW = 965, 300                      # bloques de dispositivos
DR = DX + DW
X_SCL, X_SDA = 925, 945                # buses I2C
X_GND, X_V3, X_5VB, X_BATV = 1300, 1335, 1385, 1700
Y_BAT, Y_GND = 35, 190                 # pasillos superiores
LANE0, LANE_DX = 712, 8


def yc(pin):
    return STRIP_TOP + (pin - 1) * RH + 12


class Wire:
    def __init__(self, net, pts):
        self.net, self.pts = net, pts

    def segs(self):
        return list(zip(self.pts[:-1], self.pts[1:]))


wires, dots, blocks, terms, texts = [], [], [], [], []


def block(x, y, w, h, title, sub=None, fill="#ffffff", ty=19):
    blocks.append((x, y, w, h, title, sub, fill, ty))


def term(x, y, net, label, side):
    terms.append((x, y, net, label, side))


# --- dispositivos (calcula posiciones de terminales) -----------------------------
TOF_Y0, TOF_P, TOF_H = 282, 98, 88
tof = {}   # n -> dict(sda, scl, xsh, vin, gnd)  (y de cada terminal)
tof_order = [(4, "atrás", "0x33"), (3, "frente-der.", "0x32"),
             (2, "frente-izq.", "0x31"), (1, "frente-centro", "0x30")]
for k, (n, pos, addr) in enumerate(tof_order):
    y = TOF_Y0 + k * TOF_P
    block(DX, y, DW, TOF_H, f"ToF-{n}  {pos}", f"VL53L0X  ·  {addr}")
    tof[n] = dict(y=y, sda=y + 40, scl=y + 58, xsh=y + 76, vin=y + 40, gnd=y + 62)
    term(DX, y + 40, "SDA", "SDA", "L"); term(DX, y + 58, "SCL", "SCL", "L")
    term(DX, y + 76, "XSH", "XSHUT", "L")
    term(DR, y + 40, "V3", "VIN 3.3 V", "R"); term(DR, y + 62, "GND", "GND", "R")

LCD_Y = TOF_Y0 + 4 * TOF_P + 26
lcd_terms = [(2, "V5", "5 V"), (6, "GND", "GND"), (13, "SPI", "RST"), (15, "SPI", "DC / RS"),
             (17, "V3", "3.3 V"), (19, "SPI", "SI (MOSI)"), (23, "SPI", "SCK"), (24, "SPI", "CS")]
LCD_H = 52 + len(lcd_terms) * 20 + 8
block(DX, LCD_Y, DW, LCD_H, 'LCD 3.2" ILI9341 (SPI)', "sin touch · KEY1-3 sin cablear")
lcd_y = {}
for i, (pin, net, lab) in enumerate(lcd_terms):
    y = LCD_Y + 60 + i * 20
    lcd_y[pin] = y
    term(DX, y, net, lab, "L")

SRV_Y0 = LCD_Y + LCD_H + 40
srv = {}
for i, n in enumerate((1, 2)):
    y = SRV_Y0 + i * 80
    block(DX, y, DW, 66, f"Servo {n}  (brazo)", "PWM 50 Hz")
    srv[n] = dict(sig=y + 42, vp=y + 28, gnd=y + 50)
    term(DX, y + 42, "SRV", "SEÑAL", "L")
    term(DR, y + 28, "V5", "V+ 5 V", "R"); term(DR, y + 50, "GND", "GND", "R")

TB_Y = SRV_Y0 + 2 * 80 + 40
tb_left = [(16, "BIN1"), (18, "BIN2"), (35, "AIN1"), (36, "PWMA"), (37, "STBY"),
           (38, "AIN2"), (40, "PWMB")]
TB_H = 42 + len(tb_left) * 22 + 12
block(DX, TB_Y, DW, TB_H, "TB6612FNG (SparkFun)", "driver de motores · unir todos los GND")
tb_y = {}
for i, (pin, lab) in enumerate(tb_left):
    y = TB_Y + 52 + i * 22
    tb_y[pin] = y
    term(DX, y, "MOT", lab, "L")
TB_VCC, TB_VM, TB_GND = TB_Y + 52, TB_Y + 80, TB_Y + 108
term(DR, TB_VCC, "V3", "VCC 3.3 V", "R"); term(DR, TB_VM, "VM", "VM", "R"); term(DR, TB_GND, "GND", "GND", "R")
TB_B = TB_Y + TB_H
motors = [("Motor izq.", DX, ("A01", DX + 40), ("A02", DX + 110)),
          ("Motor der.", DX + 160, ("B01", DX + 200), ("B02", DX + 270))]
MOT_Y = TB_B + 44
for name, mx, (n1, x1), (n2, x2) in motors:
    block(mx, MOT_Y, 140, 66, name, "FA-130 · oruga", ty=40)
    wires.append(Wire("VM", [(x1, TB_B), (x1, MOT_Y)])); wires.append(Wire("VM", [(x2, TB_B), (x2, MOT_Y)]))
    term(x1, TB_B, "VM", n1, "B"); term(x2, TB_B, "VM", n2, "B")
    term(x1, MOT_Y, "VM", "M+", "T"); term(x2, MOT_Y, "VM", "M-", "T")

# --- conversores a la derecha ---------------------------------------------------
CX, CW, CH = 1450, 170, 104
bec2_y = (srv[1]["vp"] + srv[2]["vp"]) // 2 - 62
block(CX, bec2_y, CW, CH, "BEC 2 · 4.8-5 V", "≥ 3 A · solo servos")
b2_out = b2_in = bec2_y + 62
b2_gnd = bec2_y + 86
term(CX, b2_out, "V5", "OUT+", "L"); term(CX, b2_gnd, "GND", "GND", "L"); term(CX + CW, b2_in, "BAT", "IN+", "R")
buck_y = TB_VM - 62
block(CX, buck_y, CW, CH, "Buck 3 V", "≥ 2 A · motores FA-130")
bk_out = bk_in = buck_y + 62
bk_gnd = buck_y + 86
term(CX, bk_out, "VM", "OUT+", "L"); term(CX, bk_gnd, "GND", "GND", "L"); term(CX + CW, bk_in, "BAT", "IN+", "R")

# --- fuente superior izquierda ---------------------------------------------------
Y_P, Y_N = 100, 135                    # BAT+ / BAT-
block(20, 45, 150, 108, "LiPo 2S", "6000 mAh · 7.4 V")
term(170, Y_P, "BAT", "BAT+", "R"); term(170, Y_N, "GND", "BAT-", "R")
block(190, Y_P - 16, 62, 32, "SW+F", "", ty=20)
BEC1_X = 300
block(BEC1_X, 45, 130, 108, "BEC 1", "5.1 V ≥ 3 A · Pi")
term(BEC1_X, Y_P, "BAT", "IN+", "L"); term(BEC1_X + 130, Y_P, "V5", "OUT+", "R"); term(BEC1_X + 130, Y_N, "GND", "GND", "R")
BEC1_OUT = BEC1_X + 130

# --- cables fijos ----------------------------------------------------------------
# baterias / pasillos
J1 = 275
wires.append(Wire("BAT", [(170, Y_P), (190, Y_P)])); wires.append(Wire("BAT", [(252, Y_P), (BEC1_X, Y_P)]))
wires.append(Wire("BAT", [(J1, Y_P), (J1, Y_BAT), (X_BATV, Y_BAT), (X_BATV, bk_in)]))
dots.append((J1, Y_P, "BAT"))
wires.append(Wire("BAT", [(CX + CW, b2_in), (X_BATV, b2_in)])); dots.append((X_BATV, b2_in, "BAT"))
wires.append(Wire("BAT", [(CX + CW, bk_in), (X_BATV, bk_in)])); dots.append((X_BATV, bk_in, "BAT"))
# GND: bateria -> pasillo -> bus de dispositivos
wires.append(Wire("GND", [(170, Y_N), (185, Y_N), (185, Y_GND), (X_GND, Y_GND), (X_GND, max(TB_GND, bk_gnd) + 30)]))
dots.append((X_GND, Y_GND, "GND"))
wires.append(Wire("GND", [(BEC1_OUT, Y_N), (440, Y_N), (440, Y_GND)])); dots.append((440, Y_GND, "GND"))
# GND de la Pi (pin 9) al pasillo
wires.append(Wire("GND", [(STRIP_X, yc(9)), (400, yc(9)), (400, Y_GND)])); dots.append((400, Y_GND, "GND"))
# 5 V BEC1 -> pin 4
X_5V1 = 460
wires.append(Wire("V5", [(BEC1_OUT, Y_P), (X_5V1, Y_P), (X_5V1, yc(4)), (STRIP_X, yc(4))]))
# 3V3: pin 1 -> bus
wires.append(Wire("V3", [(STRIP_R, yc(1)), (X_V3, yc(1)), (X_V3, TB_VCC)]))
for n in tof:
    y = tof[n]["vin"]
    wires.append(Wire("V3", [(DR, y), (X_V3, y)])); dots.append((X_V3, y, "V3"))
wires.append(Wire("V3", [(DR, TB_VCC), (X_V3, TB_VCC)]))
# GND bus taps
for n in tof:
    y = tof[n]["gnd"]
    wires.append(Wire("GND", [(DR, y), (X_GND, y)])); dots.append((X_GND, y, "GND"))
for n in srv:
    y = srv[n]["gnd"]
    wires.append(Wire("GND", [(DR, y), (X_GND, y)])); dots.append((X_GND, y, "GND"))
wires.append(Wire("GND", [(DR, TB_GND), (X_GND, TB_GND)])); dots.append((X_GND, TB_GND, "GND"))
wires.append(Wire("GND", [(CX, b2_gnd), (X_GND, b2_gnd)])); dots.append((X_GND, b2_gnd, "GND"))
wires.append(Wire("GND", [(CX, bk_gnd), (X_GND, bk_gnd)])); dots.append((X_GND, bk_gnd, "GND"))
# 5V-B: BEC2 -> servos
wires.append(Wire("V5", [(CX, b2_out), (X_5VB, b2_out), (X_5VB, srv[1]["vp"])]))
wires.append(Wire("V5", [(X_5VB, b2_out), (X_5VB, srv[2]["vp"])]))
for n in srv:
    y = srv[n]["vp"]
    wires.append(Wire("V5", [(DR, y), (X_5VB, y)])); dots.append((X_5VB, y, "V5"))
dots.append((X_5VB, b2_out, "V5"))
# VM: buck -> TB6612
wires.append(Wire("VM", [(CX, bk_out), (DR, TB_VM)] if bk_out == TB_VM else [(CX, bk_out), (X_5VB + 25, bk_out), (X_5VB + 25, TB_VM), (DR, TB_VM)]))
# I2C
sda_ys = [tof[n]["sda"] for n in tof]; scl_ys = [tof[n]["scl"] for n in tof]
wires.append(Wire("SDA", [(STRIP_R, yc(3)), (X_SDA, yc(3))]))
wires.append(Wire("SDA", [(X_SDA, min(sda_ys)), (X_SDA, max(sda_ys))])); dots.append((X_SDA, yc(3), "SDA"))
wires.append(Wire("SCL", [(STRIP_R, yc(5)), (X_SCL, yc(5))]))
wires.append(Wire("SCL", [(X_SCL, min(scl_ys)), (X_SCL, max(scl_ys))])); dots.append((X_SCL, yc(5), "SCL"))
for y in sda_ys:
    wires.append(Wire("SDA", [(X_SDA, y), (DX, y)])); dots.append((X_SDA, y, "SDA"))
for y in scl_ys:
    wires.append(Wire("SCL", [(X_SCL, y), (DX, y)])); dots.append((X_SCL, y, "SCL"))

# --- cables Pi -> dispositivo con carriles ----------------------------------------
lane_specs = []  # (pin, net, y_destino)
for pin, net, lab in lcd_terms:
    lane_specs.append((pin, net, lcd_y[pin]))
for pin, lab in tb_left:
    lane_specs.append((pin, "MOT", tb_y[pin]))
lane_specs += [(32, "SRV", srv[1]["sig"]), (33, "SRV", srv[2]["sig"])]
lane_specs += [(7, "XSH", tof[4]["xsh"]), (22, "XSH", tof[3]["xsh"]),
               (29, "XSH", tof[2]["xsh"]), (31, "XSH", tof[1]["xsh"])]


def lane_pts(spec, lane_x):
    pin, net, yd = spec
    ys = yc(pin)
    pts = [(STRIP_R, ys), (lane_x, ys), (lane_x, yd), (DX, yd)]
    out = [pts[0]]
    for p in pts[1:]:
        if p != out[-1]:
            out.append(p)
    return out


def _hv(ws):
    """Segmentos horizontales y verticales (con su red) de una lista de cables."""
    hs, vs = [], []
    for w in ws:
        for (p, q) in w.segs():
            if p[1] == q[1]:
                hs.append((w.net, min(p[0], q[0]), max(p[0], q[0]), p[1]))
            elif p[0] == q[0]:
                vs.append((w.net, p[0], min(p[1], q[1]), max(p[1], q[1])))
    return hs, vs


def count_cross(hs, vs):
    n = 0
    for (na, x1, x2, y) in hs:
        for (nb, x, y1, y2) in vs:
            if na != nb and x1 < x < x2 and y1 < y < y2:
                n += 1
    return n


def optimise_lanes(seed=7, restarts=10):
    rnd = random.Random(seed)
    k = len(lane_specs)
    fixed_h, fixed_v = _hv(wires)
    best, best_c = None, 10 ** 9

    def cost(perm):
        lh, lv = _hv([Wire(s[1], lane_pts(s, LANE0 + LANE_DX * perm[i])) for i, s in enumerate(lane_specs)])
        return count_cross(lh, fixed_v) + count_cross(fixed_h, lv) + count_cross(lh, lv)

    for _ in range(restarts):
        perm = list(range(k)); rnd.shuffle(perm)
        c = cost(perm)
        improved = True
        while improved:
            improved = False
            for i in range(k):
                for j in range(i + 1, k):
                    perm[i], perm[j] = perm[j], perm[i]
                    c2 = cost(perm)
                    if c2 < c:
                        c, improved = c2, True
                    else:
                        perm[i], perm[j] = perm[j], perm[i]
        if c < best_c:
            best, best_c = perm[:], c
    return best, best_c


perm, ncross = optimise_lanes()
lane_wires = []
for i, s in enumerate(lane_specs):
    w = Wire(s[1], lane_pts(s, LANE0 + LANE_DX * perm[i]))
    lane_wires.append(w); wires.append(w)

# --- salida SVG -------------------------------------------------------------------
R_HOP = 4


def wire_path(w, others):
    d = ""
    pts = w.pts
    x, y = pts[0]
    d += f"M{x},{y}"
    for (p, q) in zip(pts[:-1], pts[1:]):
        if p[1] == q[1]:
            xs = []
            for o in others:
                if o is w or o.net == w.net:
                    continue
                for (r, s) in o.segs():
                    if r[0] == s[0] and min(p[0], q[0]) + R_HOP + 1 < r[0] < max(p[0], q[0]) - R_HOP - 1 \
                            and min(r[1], s[1]) < p[1] < max(r[1], s[1]):
                        xs.append(r[0])
            right = q[0] > p[0]
            xs.sort(reverse=not right)
            for cx in xs:
                a, b = (cx - R_HOP, cx + R_HOP) if right else (cx + R_HOP, cx - R_HOP)
                d += f" L{a},{p[1]} A{R_HOP},{R_HOP} 0 0 {1 if right else 0} {b},{p[1]}"
        d += f" L{q[0]},{q[1]}"
    return d


out = []
add = out.append
add(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
    f'font-family="{FONT}">')
add(f'<title>Wally: diagrama de conexión pin a pin</title>')
add(f'<rect width="{W}" height="{H}" fill="#ffffff"/>')

# titulo y etiquetas de pasillos
add('<text x="600" y="98" font-size="26" font-weight="bold" fill="#111">Wally · Diagrama de conexión pin a pin</text>')
add('<text x="600" y="124" font-size="13" fill="#444">Raspberry Pi 4 → LCD, TB6612 + motores, servos y 4 sensores ToF · '
    'pines = número físico del header de 40 pines</text>')
add(f'<text x="640" y="{Y_BAT - 6}" font-size="12" fill="{NETS["BAT"][0]}" font-weight="bold">BAT+ 7.4 V (tras interruptor + fusible)</text>')
add(f'<text x="640" y="{Y_GND - 6}" font-size="12" fill="{NETS["GND"][0]}" font-weight="bold">GND común (batería, BEC, Pi, módulos)</text>')

# cables
for w in wires:
    color, width, dash, _ = NETS[w.net]
    da = f' stroke-dasharray="{dash}"' if dash else ""
    add(f'<path d="{wire_path(w, wires)}" fill="none" stroke="{color}" stroke-width="{width}" '
        f'stroke-linejoin="round" stroke-linecap="butt"{da}/>')
for (x, y, net) in dots:
    add(f'<circle cx="{x}" cy="{y}" r="4" fill="{NETS[net][0]}"/>')

# etiquetas de buses
add(f'<text x="{X_V3}" y="{yc(1) - 8}" font-size="11.5" font-weight="bold" fill="{NETS["V3"][0]}" text-anchor="middle">3.3 V</text>')
add(f'<text x="{X_GND - 6}" y="{Y_GND + 18}" font-size="11.5" font-weight="bold" fill="{NETS["GND"][0]}" text-anchor="end">GND</text>')
add(f'<text x="{X_5VB}" y="{srv[1]["vp"] - 12}" font-size="11.5" font-weight="bold" fill="{NETS["V5"][0]}" text-anchor="middle">5 V (BEC 2)</text>')
add(f'<text x="{(CX + DR) // 2 + 20}" y="{TB_VM - 8}" font-size="11.5" font-weight="bold" fill="{NETS["VM"][0]}" text-anchor="middle">VM 3 V</text>')
add(f'<text x="{X_BATV + 8}" y="{Y_BAT + 22}" font-size="11.5" font-weight="bold" fill="{NETS["BAT"][0]}">BAT+ 7.4 V</text>')

# tira de pines de la Pi
add(f'<text x="{STRIP_X}" y="{STRIP_TOP - 12}" font-size="15" font-weight="bold" fill="#111">'
    f'Raspberry Pi 4 · header GPIO</text>')
for pin, gpio, fun, grp in PINS:
    y = STRIP_TOP + (pin - 1) * RH
    fill = GROUP_FILL[grp]
    tc = "#222" if grp in GROUP_TEXT_DARK else "#fff"
    add(f'<rect x="{STRIP_X}" y="{y}" width="{STRIP_W}" height="{RH - 2}" fill="#fafafa" stroke="#bdbdbd"/>')
    add(f'<rect x="{STRIP_X}" y="{y}" width="30" height="{RH - 2}" fill="{fill}"/>')
    add(f'<text x="{STRIP_X + 15}" y="{y + 16}" font-size="12" font-weight="bold" fill="{tc}" text-anchor="middle">{pin}</text>')
    add(f'<text x="{STRIP_X + 36}" y="{y + 16}" font-size="11.5" fill="#222">{escape(gpio)}</text>')
    add(f'<text x="{STRIP_R - 5}" y="{y + 16}" font-size="11.5" fill="#222" font-weight="bold" text-anchor="end">{escape(fun)}</text>')
    # bornes de la tira
    used = grp not in ("free", "rsv", "gnd0")
    if used:
        col = GROUP_FILL[grp]
        add(f'<circle cx="{STRIP_R}" cy="{yc(pin)}" r="3.6" fill="{col}"/>')
add(f'<circle cx="{STRIP_X}" cy="{yc(4)}" r="3.6" fill="{NETS["V5"][0]}"/>')
add(f'<circle cx="{STRIP_X}" cy="{yc(9)}" r="3.6" fill="{NETS["GND"][0]}"/>')
add(f'<text x="{STRIP_X}" y="{STRIP_TOP + 40 * RH + 16}" font-size="11" fill="#555">1: libre solo sin consola serie</text>')
add(f'<text x="{STRIP_X}" y="{STRIP_TOP + 40 * RH + 31}" font-size="11" fill="#555">2: libre tras aplicar los overlays (PINOUT.md)</text>')

# bloques
for (x, y, w, h, title, sub, fill, ty) in blocks:
    add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" fill="{fill}" stroke="#555" stroke-width="1.4"/>')
    add(f'<text x="{x + w / 2}" y="{y + ty}" font-size="13.5" font-weight="bold" fill="#111" text-anchor="middle">{escape(title)}</text>')
    if sub:
        add(f'<text x="{x + w / 2}" y="{y + ty + 16}" font-size="11" fill="#555" text-anchor="middle">{escape(sub)}</text>')
# terminales
for (x, y, net, label, side) in terms:
    col = NETS[net][0]
    add(f'<circle cx="{x}" cy="{y}" r="4.2" fill="{col}" stroke="#fff" stroke-width="1"/>')
    if side == "L":
        add(f'<text x="{x + 9}" y="{y + 4}" font-size="11.5" fill="#111">{escape(label)}</text>')
    elif side == "R":
        add(f'<text x="{x - 9}" y="{y + 4}" font-size="11.5" fill="#111" text-anchor="end">{escape(label)}</text>')
    elif side == "T":
        add(f'<text x="{x}" y="{y + 16}" font-size="11" fill="#111" text-anchor="middle">{escape(label)}</text>')
    elif side == "B":
        add(f'<text x="{x}" y="{y - 8}" font-size="11" fill="#111" text-anchor="middle">{escape(label)}</text>')

# leyenda y notas (izquierda)
LX, LY = 20, 300
add(f'<text x="{LX}" y="{LY}" font-size="15" font-weight="bold" fill="#111">Leyenda</text>')
for i, key in enumerate(["GND", "V5", "V3", "BAT", "VM", "SPI", "MOT", "SRV", "SDA", "SCL", "XSH"]):
    color, width, dash, name = NETS[key]
    y = LY + 22 + i * 22
    da = f' stroke-dasharray="{dash}"' if dash else ""
    add(f'<line x1="{LX}" y1="{y}" x2="{LX + 46}" y2="{y}" stroke="{color}" stroke-width="{width}"{da}/>')
    add(f'<text x="{LX + 58}" y="{y + 4}" font-size="12.5" fill="#222">{escape(name)}</text>')
yy = LY + 22 + 11 * 22 + 6
add(f'<circle cx="{LX + 8}" cy="{yy}" r="4" fill="#333"/><text x="{LX + 22}" y="{yy + 4}" font-size="12" fill="#222">conexión (punto)</text>')
add(f'<path d="M{LX + 210 - 30},{yy} L{LX + 210 - 4},{yy} A4,4 0 0 1 {LX + 210 + 4},{yy} L{LX + 210 + 30},{yy}" fill="none" stroke="#333" stroke-width="1.6"/>')
add(f'<text x="{LX + 210 + 36}" y="{yy + 4}" font-size="12" fill="#222">cruce sin conexión</text>')

notes = [
    "Notas",
    "• Los pines 2 y 4 (5 V) son el mismo",
    "  nodo: el BEC 1 alimenta la Pi por el 4",
    "  y el LCD toma su 5 V del 2.",
    "• Servos: BEC 2 aparte. De la Pi solo",
    "  viene la señal (pines 32 y 33).",
    "• Motores: buck de 3 V a VM. Sin buck,",
    "  VM = BAT+ y limitar el duty a ≤ 40 %.",
    "• STBY (pin 37): resistencia de 10 kΩ",
    "  a GND (motores apagados al arrancar).",
    "• ToF a 3.3 V. Se les asigna la dirección",
    "  0x30-0x33 al arrancar, con su XSHUT.",
    "• I2C: pull-ups de 1.8 kΩ ya en la Pi.",
    "• GND común en TODO el robot.",
    "• Pines 'libre': no se conectan.",
]
ny = yy + 40
for i, line in enumerate(notes):
    fs, wt = (15, "bold") if i == 0 else (12.5, "normal")
    add(f'<text x="{LX}" y="{ny + i * 19}" font-size="{fs}" font-weight="{wt}" fill="#222" xml:space="preserve">{escape(line)}</text>')

add('</svg>')
svg = "\n".join(out)
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "wiring.svg"), "w", encoding="utf-8") as f:
    f.write(svg)
print(f"wiring.svg escrito: {len(svg) // 1024} KiB, {len(wires)} cables, cruces entre carriles: {ncross}")


# --- lista de cables (markdown), derivada de los mismos datos -----------------------
COLOR_NAME = {"GND": "negro", "V5": "rojo", "V3": "naranja", "BAT": "magenta", "VM": "marrón",
              "SPI": "turquesa", "MOT": "azul", "SRV": "morado", "SDA": "verde",
              "SCL": "verde punteado", "XSH": "gris"}
PIN_GPIO = {p: g for p, g, _f, _grp in PINS}


def pin_label(p):
    return f"pin {p} ({PIN_GPIO[p]})"


def cable_tables_md():
    n = [0]

    def table(title, rows):
        out = [f"**{title}**", "", "| # | Desde | Hasta | Color | Nota |", "|---|---|---|---|---|"]
        for frm, to, net, note in rows:
            n[0] += 1
            out.append(f"| {n[0]:02d} | {frm} | {to} | {COLOR_NAME[net]} | {note} |")
        return out + [""]

    power = [
        ("LiPo 2S BAT+", "interruptor + fusible → IN+ del BEC 1, BEC 2 y Buck 3 V", "BAT", "cable grueso (≥ 18 AWG)"),
        ("LiPo 2S BAT−", "GND común", "GND", ""),
        ("BEC 1 OUT+", pin_label(4), "V5", "alimenta la Pi; no conectar a la vez el USB-C"),
        ("BEC 1 GND", "GND común", "GND", ""),
        (pin_label(9), "GND común", "GND", "masa de la Pi"),
        ("BEC 2 OUT+", "Servo 1 V+ y Servo 2 V+", "V5", "solo servos"),
        ("BEC 2 GND", "GND común", "GND", ""),
        ("Buck 3 V OUT+", "TB6612 VM", "VM", "o VM = BAT+ con duty ≤ 40 %"),
        ("Buck 3 V GND", "GND común", "GND", ""),
        ("GND de ToF-1 a ToF-4", "GND común", "GND", "4 cables"),
        ("GND de Servo 1 y Servo 2", "GND común", "GND", "2 cables"),
        ("TB6612 GND", "GND común", "GND", "unir todos los GND del módulo"),
    ]
    dest = {}
    for pin, net, lab in lcd_terms:
        dest[pin] = (f"LCD {lab}", net)
    for pin, lab in tb_left:
        dest[pin] = (f"TB6612 {lab}", "MOT")
    dest[32], dest[33] = ("Servo 1 SEÑAL", "SRV"), ("Servo 2 SEÑAL", "SRV")
    dest[7], dest[22], dest[29], dest[31] = (("ToF-4 XSHUT", "XSH"), ("ToF-3 XSHUT", "XSH"),
                                              ("ToF-2 XSHUT", "XSH"), ("ToF-1 XSHUT", "XSH"))
    dest[3] = ("SDA de los 4 ToF (en paralelo)", "SDA")
    dest[5] = ("SCL de los 4 ToF (en paralelo)", "SCL")
    dest[1] = ("riel 3.3 V: VIN de los 4 ToF + VCC del TB6612", "V3")
    notes = {2: "5 V del BEC 1 (mismo nodo que el pin 4)", 6: "masa del LCD", 17: "3.3 V del LCD",
             1: "también sirve el pin 17", 37: "+ resistencia 10 kΩ a GND"}
    pi_rows = [(pin_label(p), dest[p][0], dest[p][1], notes.get(p, "")) for p in sorted(dest)]
    mot_rows = [("TB6612 A01", "Motor izq. M+", "VM", ""), ("TB6612 A02", "Motor izq. M−", "VM", ""),
                ("TB6612 B01", "Motor der. M+", "VM", ""), ("TB6612 B02", "Motor der. M−", "VM", "")]
    lines = table("Alimentación y masa", power) + table("Desde el header de la Pi", pi_rows) \
        + table("Salidas de motor", mot_rows)
    lines.append(f"Total: {n[0]} cables.")
    return "\n".join(lines)


def update_pinout_md(path):
    if not os.path.exists(path):
        return False
    text = open(path, encoding="utf-8").read()
    a, b = "<!-- CABLES:BEGIN -->", "<!-- CABLES:END -->"
    if a not in text or b not in text:
        return False
    i, j = text.index(a) + len(a), text.index(b)
    open(path, "w", encoding="utf-8").write(text[:i] + "\n\n" + cable_tables_md() + "\n" + text[j:])
    return True


if update_pinout_md(os.path.join(os.path.dirname(here), "PINOUT.md")):
    print("PINOUT.md: lista de cables actualizada")
