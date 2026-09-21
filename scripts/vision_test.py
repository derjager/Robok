#!/usr/bin/env python3
"""Puesta en marcha de la visión: qué ve Wally, a quién reconoce y con qué puntuación. No mueve nada.

  .venv/bin/python scripts/vision_test.py foto1.jpg foto2.jpg     # analiza imágenes sueltas
  .venv/bin/python scripts/vision_test.py --live 30               # 30 s con la cámara en vivo
  .venv/bin/python scripts/vision_test.py --live 30 --fps 6

Usa las identidades que ya enseñaste (data/vision) y muestra, para cada gato o persona, el nombre aceptado o el
mejor candidato con su puntuación aunque no llegue al umbral. Así se ve si `cat_threshold` (0.65) o
`person_threshold` (0.40) de config.toml conviene subirlo (se confunde) o bajarlo (no lo reconoce).
Requiere los modelos: .venv/bin/python scripts/get_models.py
"""
import argparse
import io
import sys
import time

from robok.config import VisionCfg, load_config
from robok.hal.camera import RpicamCamera
from robok.services.vision import Vision
from robok.vision.gallery import Gallery
from robok.vision.models import load_backends, missing, resolve_dir


def line(t: dict) -> str:
    name = t["identity"]["name"] if t["identity"] else None
    who = f"{name} ({t['identity']['score']:.2f})" if name else "desconocido"
    if not name and t["candidate"]:
        who += f", parecido a {t['candidate']['name']} ({t['candidate']['score']:.2f})"
    x0, y0, x1, y1 = t["box"]
    return f"  #{t['id']} {t['kind_es']:7s} conf {t['score']:.2f}  caja x {x0:.2f}-{x1:.2f} y {y0:.2f}-{y1:.2f}  -> {who}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("images", nargs="*", help="imágenes a analizar")
    ap.add_argument("--live", type=float, metavar="SEGUNDOS", help="analizar la cámara en vivo")
    ap.add_argument("--fps", type=float, help="análisis por segundo (por defecto el de config.toml)")
    args = ap.parse_args()
    if not args.images and not args.live:
        ap.error("pasa imágenes o --live SEGUNDOS")
    cfg = load_config().vision
    if args.fps:
        cfg = VisionCfg(**{**cfg.__dict__, "fps": args.fps})
    gone = missing(cfg)
    if any(m.key == "detector" for m in gone):
        sys.exit("Faltan los modelos: .venv/bin/python scripts/get_models.py")
    det, emb, faces, desc = load_backends(cfg)
    gallery = Gallery(resolve_dir(cfg.data_dir))
    print(f"Modelos: {desc}")
    print("Identidades: " + (", ".join(f"{i['name']} ({i['kind']}, {len(i['samples'])} muestras)" for i in gallery.list()) or "ninguna todavía"))
    vision = Vision(cfg, None, gallery, det, emb, faces, lambda x, y: None, unavailable=None, desc=desc)
    t = 0.0

    if args.images:
        from PIL import Image
        for path in args.images:
            buf = io.BytesIO()
            Image.open(path).convert("RGB").save(buf, "JPEG", quality=92)
            res = None
            for _ in range(4):                       # varios pasadas: la identidad se decide por votos
                t += 0.3
                res = vision.process_frame(buf.getvalue(), t)
            print(f"\n{path}: {len(res['tracks'])} objeto(s), detector {res['detect_ms']} ms")
            for tr in res["tracks"]:
                print(line(tr))
        return 0

    cam = RpicamCamera(load_config().camera)
    cam.acquire()
    end, last = time.monotonic() + args.live, 0
    print(f"\nMirando la cámara {args.live:.0f} s (Ctrl+C para parar)…")
    try:
        while time.monotonic() < end:
            fr = cam.latest()
            if fr is None or fr[0] == last or (cam.latest_age() or 9) > 1.5:
                time.sleep(0.03)
                continue
            last = fr[0]
            t0 = time.monotonic()
            res = vision.process_frame(fr[1], t0)
            print(f"\n[{time.strftime('%H:%M:%S')}] {len(res['tracks'])} objeto(s), ciclo {int((time.monotonic() - t0) * 1000)} ms")
            for tr in res["tracks"]:
                print(line(tr))
            time.sleep(max(0.0, 1.0 / cfg.fps - (time.monotonic() - t0)))
    except KeyboardInterrupt:
        pass
    finally:
        cam.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
