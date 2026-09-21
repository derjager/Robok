#!/usr/bin/env python3
"""Descarga los modelos de la visión a models/ y verifica su SHA-256.

  .venv/bin/python scripts/get_models.py          # baja lo que falte (~48 MB en total)
  .venv/bin/python scripts/get_models.py --check  # solo verifica los que ya están

Un archivo cuyo hash no coincide se rechaza y se borra: es el modelo que se probó, no otro.
"""
import argparse
import os
import sys
import urllib.request

from robok.config import VisionCfg
from robok.vision.models import MODELS, models_path, sha256_of


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="no descargar nada: solo verificar")
    args = ap.parse_args()
    base = models_path(VisionCfg())
    base.mkdir(parents=True, exist_ok=True)
    bad = 0
    for m in MODELS:
        dest = base / m.filename
        if dest.is_file():
            ok = sha256_of(dest) == m.sha256
            print(f"{'OK ' if ok else 'MAL'} {m.filename}: {m.what}" + ("" if ok else "  (hash distinto: bórralo y vuelve a correr)"))
            bad += 0 if ok else 1
            continue
        if args.check:
            print(f"FALTA {m.filename}: {m.what}")
            bad += 1
            continue
        print(f"bajando {m.filename} ({m.what}, licencia {m.licence})…", flush=True)
        tmp = dest.with_name(dest.name + ".part")
        try:
            with urllib.request.urlopen(m.url, timeout=120) as r, tmp.open("wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
            if sha256_of(tmp) != m.sha256:
                print(f"  ✗ el hash no coincide con el esperado; se descarta {m.filename}")
                tmp.unlink()
                bad += 1
                continue
            os.replace(tmp, dest)
            print(f"  ✓ {dest.stat().st_size // 1024} KiB")
        except Exception as e:
            print(f"  ✗ {type(e).__name__}: {e}")
            tmp.unlink(missing_ok=True)
            bad += 1
    print("\nModelos listos." if not bad else f"\n{bad} problema(s).")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
