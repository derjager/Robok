#!/usr/bin/env python3
"""Puesta en marcha de los sensores ToF VL53L0X. Solo toca los 4 XSHUT (GPIO 6, 5, 25, 4) y el I2C1; no mueve nada.

  .venv/bin/python scripts/tof_test.py                    # los 4 sensores, 10 s de lecturas
  .venv/bin/python scripts/tof_test.py --sensors front    # solo el que ya cableaste
  .venv/bin/python scripts/tof_test.py --seconds 30

Qué hace: 1) escanea el bus I2C1 (solo lectura), 2) enciende los sensores uno por uno asignando 0x30-0x33,
3) imprime las distancias en vivo. Si un sensor no arranca dice por qué. Con la mano frente a cada sensor
debe cambiar SOLO su columna: así compruebas que cada XSHUT va al sensor correcto (frente, frente-izquierda,
frente-derecha, atrás; ver PINOUT.md §4.4).
"""
import argparse
import sys
import time

from robok.config import TOF_NAMES, Pins, TofCfg
from robok.hal.tof import TofArray


def scan(bus: int) -> list[int]:
    from smbus2 import SMBus
    found = []
    with SMBus(bus) as b:
        for a in range(0x08, 0x78):
            try:
                b.read_byte(a)
                found.append(a)
            except OSError:
                pass
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sensors", default=",".join(TOF_NAMES), help=f"lista separada por comas de {list(TOF_NAMES)}")
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--bus", type=int, default=1)
    args = ap.parse_args()
    names = tuple(s.strip() for s in args.sensors.split(",") if s.strip())
    try:
        cfg = TofCfg(enabled=True, sensors=names, i2c_bus=args.bus)
    except ValueError as e:
        sys.exit(str(e))

    try:
        before = scan(args.bus)
    except OSError as e:
        sys.exit(f"No se pudo abrir /dev/i2c-{args.bus}: {e}\n"
                 "Activa el I2C: sudo raspi-config nonint do_i2c 0   (y reinicia)")
    print(f"I2C{args.bus} antes de encender los sensores: {[hex(a) for a in before] or 'nada responde'}")
    if before:
        print("  (los VL53L0X de fábrica nacen en 0x29; con XSHUT sin conectar pueden verse ya aquí)")

    arr = TofArray(cfg, Pins())
    print(f"\nEncendiendo {', '.join(names)} (XSHUT en GPIO {[Pins().tof_xshut[TOF_NAMES.index(n)] for n in names]})…")
    arr.start()
    try:
        print(f"Direcciones: " + ", ".join(f"{n}=0x{cfg.base_address + TOF_NAMES.index(n):02X}" for n in names))
        print(f"I2C{args.bus} con los sensores encendidos: {[hex(a) for a in scan(args.bus)] or 'nada'}")
        bad = arr.problems()
        for n, why in bad.items():
            print(f"  ✗ {n}: {why}")
        if bad:
            print("\nRevisa: VIN a 3.3 V, GND, SDA/SCL (pines 3 y 5) y el cable XSHUT de ese sensor.")
        print("\n" + "  ".join(f"{n:>12}" for n in names))
        end = time.monotonic() + args.seconds
        while time.monotonic() < end:
            snap = arr.snapshot()
            cells = []
            for n in names:
                r = snap[n]
                cells.append(f"{r.cm:>9.1f} cm" if r.state == "ok" else f"{r.state:>12}")
            print("\r" + "  ".join(cells), end="", flush=True)
            time.sleep(0.1)
        print()
    except KeyboardInterrupt:
        print("\nInterrumpido.")
    finally:
        arr.close()                      # XSHUT bajos y GPIO liberados
        print("Sensores apagados y GPIO liberados.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
