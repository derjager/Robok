#!/usr/bin/env python3
"""Puesta en marcha de los motores REALES. Léelo antes de ejecutarlo con --go.

  .venv/bin/python scripts/motor_test.py            # solo muestra el plan; NO toca ningún pin
  .venv/bin/python scripts/motor_test.py --go       # prueba real, paso a paso, con tu confirmación

Antes de --go:
  - Las orugas EN EL AIRE (el robot sobre un soporte) y nada que se pueda enredar.
  - TB6612 cableado según PINOUT.md y alimentado (VM). Si VM es la batería de 7.4 V, deja la potencia
    baja: los FA-130 son de 3 V. Con el buck de 3 V puedes subir --duty.
  - Una mano lista en la batería/interruptor. Ctrl+C también para todo.

Cada paso mueve UN motor a baja potencia unos segundos y te pregunta qué pasó. Al final imprime las
líneas para config.toml ([motors]) que corrigen el sentido o el intercambio de lados.
"""
import argparse
import sys
import time

from robok.config import MotorsCfg, Pins
from robok.hal.motors import LgpioBackend, Tb6612Motors, infer_wiring

STEPS = [
    ("izquierda", "adelante", 1, 0),
    ("derecha", "adelante", 0, 1),
    ("izquierda", "atrás", -1, 0),
    ("derecha", "atrás", 0, -1),
]


def ask(prompt: str, valid: str) -> str:
    while True:
        a = input(f"{prompt} ").strip().lower()
        if a and a in valid:
            return a
        print(f"   responde una de: {', '.join(valid)}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--go", action="store_true", help="ejecutar la prueba real (sin esto solo muestra el plan)")
    ap.add_argument("--duty", type=float, default=0.25, help="potencia de la prueba, 0.05-0.6 (por defecto 0.25)")
    ap.add_argument("--seconds", type=float, default=1.5, help="duración de cada movimiento")
    ap.add_argument("--chip", type=int, default=0, help="gpiochip (0 en la Pi 4)")
    args = ap.parse_args()
    if not 0.05 <= args.duty <= 0.6:
        sys.exit("--duty debe estar entre 0.05 y 0.6 (esta prueba nunca usa más)")
    if not 0.3 <= args.seconds <= 4:
        sys.exit("--seconds debe estar entre 0.3 y 4")

    pins = Pins()
    print("Pines (BCM):", f"PWMA={pins.motor_pwma} AIN1={pins.motor_ain1} AIN2={pins.motor_ain2} "
          f"PWMB={pins.motor_pwmb} BIN1={pins.motor_bin1} BIN2={pins.motor_bin2} STBY={pins.motor_stby}")
    print(f"Potencia de prueba: {args.duty:.0%} durante {args.seconds} s por paso.")
    print("Pasos:", "; ".join(f"{i + 1}) {lado} {sentido}" for i, (lado, sentido, *_) in enumerate(STEPS)))
    if not args.go:
        print("\nModo de solo lectura: no se tocó ningún pin. Ejecuta con --go para la prueba real.")
        return 0

    print("\n⚠  Las orugas deben estar en el aire y la mano lista para cortar la batería.")
    if input("Escribe SI para continuar: ").strip() != "SI":
        print("Cancelado.")
        return 1

    cfg = MotorsCfg(enabled=True, max_duty=args.duty)    # sin inversiones: se mide el cableado crudo
    gpio = LgpioBackend(args.chip)
    motors = Tb6612Motors(gpio, pins, cfg)
    obs: dict[str, tuple[str, str]] = {}
    try:
        print("\nEstado inicial: STBY bajo, PWM en 0. Nada debe moverse.")
        time.sleep(1.0)
        for i, (lado, sentido, sl, sr) in enumerate(STEPS, 1):
            input(f"\nPaso {i}/{len(STEPS)}: {lado} {sentido}. Enter para mover (Ctrl+C para abortar)… ")
            motors.set_speeds(sl * args.duty, sr * args.duty)
            time.sleep(args.seconds)
            motors.stop()
            if i <= 2:   # solo los dos primeros sirven para deducir el cableado
                track = ask("¿Qué oruga giró? i=izquierda d=derecha n=ninguna:", "idn")
                if track == "n":
                    print("   Ninguna se movió: revisa VM, GND común, STBY y los cables de PWM/IN. Abortando.")
                    return 2
                d = ask("¿En qué sentido? a=adelante r=atrás:", "ar")
                obs[str(i)] = (track, d)
            else:
                ask("¿Giró en sentido contrario al paso anterior? s/n:", "sn")
    except KeyboardInterrupt:
        print("\nAbortado por el usuario.")
        return 130
    finally:
        motors.close()      # STBY bajo, PWM en 0, GPIO liberados: siempre
        print("Motores detenidos y GPIO liberados.")

    result = infer_wiring(*obs["1"], *obs["2"]) if len(obs) == 2 else None
    if result is None:
        print("\nNo pude deducir el cableado (¿se movió la misma oruga dos veces?). Revisa y repite.")
        return 3
    print("\nResultado. Copia esto en config.toml:\n")
    print("[motors]")
    print("enabled = true")
    print(f"swap_sides = {str(result['swap_sides']).lower()}")
    print(f"invert_left = {str(result['invert_left']).lower()}")
    print(f"invert_right = {str(result['invert_right']).lower()}")
    print("\nDespués, arranca `python -m robok` y prueba el joystick con las orugas en el aire.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
