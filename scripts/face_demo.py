#!/usr/bin/env python3
"""Recorre todas las expresiones de Wally en el LCD real (o simulado con --sim).

Uso (desde la raíz del repo, con el venv):  .venv/bin/python scripts/face_demo.py [--sim] [--hold]
"""
import argparse
import math
import time

from robok.config import Config
from robok.core import build_robot
from robok.services.face import EXPRESSIONS


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--sim", action="store_true")
    p.add_argument("--hold", action="store_true", help="deja la última expresión en pantalla")
    p.add_argument("--seconds", type=float, default=1.6, help="tiempo por expresión")
    args = p.parse_args()

    robot = build_robot(Config(sim=args.sim))
    robot.start()
    print("pantalla:", robot.display_desc)
    try:
        for name in EXPRESSIONS:
            print(" ", name)
            robot.face.set_expression(name)
            t0 = time.time()
            while time.time() - t0 < args.seconds:      # la mirada dibuja un círculo
                a = (time.time() - t0) * 4
                robot.face.look(math.cos(a) * 0.8, math.sin(a) * 0.6)
                time.sleep(0.05)
            robot.face.look(0, 0)
        robot.face.set_expression("happy")
        time.sleep(1.0)
        robot.face.set_expression("neutral")
        time.sleep(0.5)
    finally:
        if not args.hold:
            robot.face.stop()
        robot.stop()


if __name__ == "__main__":
    main()
