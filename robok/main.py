"""Punto de entrada: `python -m robok` o el comando `robok`."""
from __future__ import annotations

import argparse
import logging
import signal
import socket

from dataclasses import replace

from robok import __version__
from robok.config import Config, load_config
from robok.core import build_robot
from robok.web.app import create_app
from robok.web.auth import resolve_token

log = logging.getLogger("robok")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="robok", description="Wally, robot tanque con cara, ojos y brazos")
    p.add_argument("--config", help="ruta de un config.toml (por defecto ./config.toml o $ROBOK_CONFIG)")
    p.add_argument("--sim", action="store_true", help="modo simulado: sin LCD ni audio reales")
    p.add_argument("--host", help="interfaz donde escucha la web")
    p.add_argument("--port", type=int, help="puerto de la web")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=f"robok {__version__}")
    return p.parse_args(argv)


def apply_overrides(cfg: Config, args: argparse.Namespace) -> Config:
    web = cfg.web
    if args.host or args.port:
        web = replace(web, host=args.host or web.host, port=args.port or web.port)
    return replace(cfg, sim=cfg.sim or args.sim, web=web)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    cfg = apply_overrides(load_config(args.config), args)
    robot = build_robot(cfg)
    token = resolve_token(cfg)
    app = create_app(robot, token)

    import uvicorn  # import tardío: las pruebas no lo necesitan

    host = socket.gethostname()
    log.info("Interfaz: http://%s.local:%d   código de acceso: %s", host, cfg.web.port, token)

    robot.start()
    robot.voice.sound("encendido")
    # sin log de acceso: la vista previa pide una imagen cada 400 ms y el token viaja en la URL
    server = uvicorn.Server(uvicorn.Config(app, host=cfg.web.host, port=cfg.web.port, log_level="info",
                                           access_log=False))
    signal.signal(signal.SIGTERM, lambda *_: setattr(server, "should_exit", True))  # systemd
    try:
        server.run()
    finally:
        robot.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
