"""API web de Wally: REST + WebSocket + interfaz estática. Todo lo que actúa exige el token."""
from __future__ import annotations

import asyncio
import io
import json
import logging
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from robok import __version__
from robok.core import Robot
from robok.services.face import EXPRESSIONS
from robok.services.vision import VisionError
from robok.services.voice import MOODS, SOUNDS
from robok.vision.gallery import ID_RE, SAMPLE_RE, GalleryError
from robok.vision.types import KIND_ES
from robok.web.auth import same

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


class FaceReq(BaseModel):
    expression: str
    hold: float | None = Field(default=None, gt=0, le=60)
    gaze: tuple[float, float] | None = None


class SayReq(BaseModel):
    text: str = Field(min_length=1, max_length=400)
    mood: str = "normal"


class SoundReq(BaseModel):
    name: str


class SimTofReq(BaseModel):
    name: str
    cm: float | None = Field(default=None, ge=0, le=1000, allow_inf_nan=False)   # None = despejado
    fail: bool = False


class VisionSettingsReq(BaseModel):
    enabled: bool | None = None
    gaze_mirror: bool | None = None


class IdentityReq(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    kind: str
    follow: bool = True          # ¿los ojos lo siguen cuando lo ven?
    teach: bool = False          # empezar a enseñarlo en cuanto se crea


class IdentityPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    follow: bool | None = None


class TeachReq(BaseModel):
    samples: int | None = Field(default=None, ge=3, le=40)


class DriveReq(BaseModel):
    x: float = Field(ge=-1, le=1, allow_inf_nan=False)              # giro, + derecha
    y: float = Field(ge=-1, le=1, allow_inf_nan=False)              # avance, + adelante
    scale: float = Field(default=1.0, ge=0, le=1, allow_inf_nan=False)


BOUNDARY = "frame"


def _put_latest(q: asyncio.Queue, item) -> None:
    """Cola de un solo elemento: si el cliente es lento se descarta el fotograma viejo, nunca se acumula."""
    if q.full():
        q.get_nowait()
    q.put_nowait(item)


async def next_frame(cam, timeout: float) -> bytes:
    """Espera el próximo fotograma nuevo de la cámara (sin bloquear el bucle de eventos)."""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()

    def cb(_seq: int, jpeg: bytes) -> None:
        loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(jpeg))

    unsub = cam.subscribe(cb)
    try:
        return await asyncio.wait_for(fut, timeout)
    finally:
        unsub()


def create_app(robot: Robot, token: str) -> FastAPI:
    app = FastAPI(title="Wally", version=__version__, docs_url=None, redoc_url=None)

    def require_token(x_wally_token: str | None = Header(default=None),
                      t: str | None = Query(default=None, alias="token")) -> None:
        if not (same(x_wally_token, token) or same(t, token)):
            raise HTTPException(status_code=401, detail="token inválido")

    auth = [Depends(require_token)]

    # --- acciones compartidas por REST y WebSocket ---------------------------------------
    def do_face(expression: str, hold: float | None, gaze) -> None:
        robot.face.set_expression(expression, hold)
        if gaze is not None:
            robot.face.look(float(gaze[0]), float(gaze[1]))

    def handle(msg: dict) -> dict:
        kind = msg.get("type")
        try:
            if kind == "face":
                gaze = msg.get("gaze")
                do_face(str(msg["expression"]), float(msg["hold"]) if msg.get("hold") else None, gaze)
            elif kind == "look":
                robot.face.look(float(msg["x"]), float(msg["y"]))
            elif kind == "say":
                return {"type": "ack", "ok": robot.voice.say(str(msg["text"]), str(msg.get("mood", "normal")))}
            elif kind == "sound":
                return {"type": "ack", "ok": robot.voice.sound(str(msg["name"]))}
            elif kind == "stop_voice":
                robot.voice.stop()
            elif kind == "drive":
                ok = robot.drive.command(float(msg["x"]), float(msg["y"]), float(msg.get("scale", 1.0)))
                return {"type": "ack", "ok": ok}
            elif kind == "stop":
                robot.drive.stop()
            elif kind == "estop":
                robot.drive.estop()
            elif kind == "estop_reset":
                robot.drive.reset_estop()
            elif kind == "ping":
                return {"type": "pong"}
            else:
                return {"type": "error", "error": f"tipo desconocido: {kind!r}"}
        except (KeyError, ValueError, TypeError, IndexError) as e:
            return {"type": "error", "error": f"{type(e).__name__}: {e}"}
        return {"type": "ack", "ok": True}

    # --- REST ------------------------------------------------------------------------------
    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "name": "Wally", "version": __version__, "sim": robot.cfg.sim}

    @app.get("/api/state", dependencies=auth)
    def state() -> dict:
        return robot.status()

    @app.get("/api/expressions", dependencies=auth)
    def expressions() -> dict:
        return {"expressions": list(EXPRESSIONS), "moods": list(MOODS), "sounds": list(SOUNDS)}

    @app.post("/api/face", dependencies=auth)
    def set_face(req: FaceReq) -> dict:
        try:
            do_face(req.expression, req.hold, req.gaze)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        return robot.face.state()

    @app.post("/api/say", dependencies=auth)
    def say(req: SayReq) -> dict:
        return {"queued": robot.voice.say(req.text, req.mood)}

    @app.post("/api/sound", dependencies=auth)
    def sound(req: SoundReq) -> dict:
        if req.name not in SOUNDS:
            raise HTTPException(status_code=422, detail=f"sonido desconocido: {req.name!r}")
        return {"queued": robot.voice.sound(req.name)}

    @app.post("/api/voice/stop", dependencies=auth)
    def stop_voice() -> dict:
        robot.voice.stop()
        return {"ok": True}

    @app.post("/api/drive", dependencies=auth)
    def drive(req: DriveReq) -> dict:
        return {"ok": robot.drive.command(req.x, req.y, req.scale), **robot.drive.status()}

    @app.post("/api/stop", dependencies=auth)
    def stop() -> dict:
        robot.drive.stop()
        return robot.drive.status()

    @app.post("/api/estop", dependencies=auth)
    def estop() -> dict:
        robot.drive.estop()
        return robot.drive.status()

    @app.post("/api/estop/reset", dependencies=auth)
    def estop_reset() -> dict:
        robot.drive.reset_estop()
        return robot.drive.status()

    @app.get("/api/face.png", dependencies=auth)
    def face_png() -> Response:
        buf = io.BytesIO()
        robot.face.frame().save(buf, "PNG")
        return Response(buf.getvalue(), media_type="image/png", headers={"Cache-Control": "no-store"})

    # --- cámara ------------------------------------------------------------------------------
    def need_camera():
        if robot.camera is None:
            raise HTTPException(status_code=404, detail="la cámara está desactivada (camera.enabled = false)")
        return robot.camera

    @app.get("/api/camera.jpg", dependencies=auth)
    async def camera_jpg() -> Response:
        cam = need_camera()
        await asyncio.to_thread(cam.acquire)
        try:
            jpeg = await next_frame(cam, 5.0)
        except asyncio.TimeoutError as e:
            raise HTTPException(status_code=503, detail=cam.status().get("error") or "la cámara no entrega imágenes") from e
        finally:
            cam.release()
        return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/api/camera.mjpg", dependencies=auth)
    async def camera_stream(frames: int | None = Query(default=None, ge=1, le=600)) -> StreamingResponse:
        """Video MJPEG para un <img>. La cámara se enciende con el primer espectador y se apaga al irse el último.
        `frames` limita el número de fotogramas (para pruebas y curl)."""
        cam = need_camera()
        loop = asyncio.get_running_loop()

        async def gen():
            await asyncio.to_thread(cam.acquire)        # dentro del generador: si el cliente nunca lee, no queda ocupada
            q: asyncio.Queue = asyncio.Queue(maxsize=1)
            unsub = cam.subscribe(lambda _s, jpeg: loop.call_soon_threadsafe(_put_latest, q, jpeg))
            sent = 0
            try:
                while frames is None or sent < frames:
                    try:
                        jpeg = await asyncio.wait_for(q.get(), timeout=10)
                    except asyncio.TimeoutError:
                        return                          # la cámara dejó de entregar: se cierra y la página reconecta
                    yield (f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpeg)}\r\n\r\n"
                           ).encode() + jpeg + b"\r\n"
                    sent += 1
            finally:
                unsub()
                cam.release()

        return StreamingResponse(gen(), media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
                                 headers={"Cache-Control": "no-store"})

    # --- visión: identidades y enseñanza -------------------------------------------------------------
    def need_vision():
        if robot.vision is None:
            raise HTTPException(status_code=404, detail="la visión está desactivada (vision.enabled = false)")
        return robot.vision

    def public_identity(i: dict) -> dict:
        return {"id": i["id"], "name": i["name"], "kind": i["kind"], "kind_es": KIND_ES[i["kind"]], "follow": i["follow"],
                "samples": [s["id"] for s in i["samples"]]}

    def gallery_call(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except GalleryError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        except VisionError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e

    def valid_id(identity_id: str) -> str:
        if not ID_RE.match(identity_id):
            raise HTTPException(status_code=404, detail="identidad desconocida")
        return identity_id

    @app.get("/api/vision", dependencies=auth)
    def vision_status() -> dict:
        return need_vision().status()

    @app.post("/api/vision/settings", dependencies=auth)
    def vision_settings(req: VisionSettingsReq) -> dict:
        v = need_vision()
        gallery_call(v.configure, req.enabled, req.gaze_mirror)
        return v.status()

    @app.get("/api/identities", dependencies=auth)
    def identities() -> dict:
        v = need_vision()
        return {"identities": [public_identity(i) for i in v.gallery.list()]}

    @app.post("/api/identities", dependencies=auth)
    def identity_create(req: IdentityReq) -> dict:
        v = need_vision()
        ident = gallery_call(v.gallery.create, req.name, req.kind, req.follow)
        out = {"identity": public_identity(ident), "teach": None}
        if req.teach:
            try:
                out["teach"] = v.start_teaching(ident["id"])
            except (VisionError, GalleryError) as e:
                out["teach_error"] = str(e)          # la identidad ya existe: se puede enseñar después
        return out

    @app.patch("/api/identities/{identity_id}", dependencies=auth)
    def identity_update(identity_id: str, req: IdentityPatch) -> dict:
        v = need_vision()
        return public_identity(gallery_call(v.gallery.update, valid_id(identity_id), req.name, req.follow))

    @app.delete("/api/identities/{identity_id}", dependencies=auth)
    def identity_delete(identity_id: str) -> dict:
        v = need_vision()
        t = v.status()["teach"]
        if t and t["identity_id"] == identity_id and t["state"] == "active":
            v.cancel_teaching()
        gallery_call(v.gallery.delete, valid_id(identity_id))
        return {"ok": True}

    @app.post("/api/identities/{identity_id}/teach", dependencies=auth)
    def identity_teach(identity_id: str, req: TeachReq | None = None) -> dict:
        v = need_vision()
        return {"teach": gallery_call(v.start_teaching, valid_id(identity_id), req.samples if req else None)}

    @app.post("/api/vision/teach/cancel", dependencies=auth)
    def teach_cancel() -> dict:
        v = need_vision()
        v.cancel_teaching()
        return v.status()

    @app.delete("/api/identities/{identity_id}/samples/{sample_id}", dependencies=auth)
    def sample_delete(identity_id: str, sample_id: str) -> dict:
        v = need_vision()
        if not SAMPLE_RE.match(sample_id):
            raise HTTPException(status_code=404, detail="muestra desconocida")
        gallery_call(v.gallery.delete_sample, valid_id(identity_id), sample_id)
        return public_identity(v.gallery.get(identity_id))

    @app.get("/api/identities/{identity_id}/samples/{sample_id}.jpg", dependencies=auth)
    def sample_thumb(identity_id: str, sample_id: str) -> FileResponse:
        v = need_vision()
        p = v.gallery.thumb_path(valid_id(identity_id), sample_id)
        if p is None:
            raise HTTPException(status_code=404, detail="miniatura desconocida")
        return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})

    # --- solo simulación: fijar distancias de los ToF simulados para probar la interfaz -------------
    @app.post("/api/sim/tof", dependencies=auth)
    def sim_tof(req: SimTofReq) -> dict:
        tof = robot.tof
        if not robot.cfg.sim or tof is None or not hasattr(tof, "set"):
            raise HTTPException(status_code=404, detail="solo existe con --sim y tof.enabled = true")
        try:
            tof.fail(req.name) if req.fail else tof.set(req.name, req.cm)
        except KeyError as e:
            raise HTTPException(status_code=422, detail=f"sensor desconocido: {req.name!r}") from e
        robot.obstacles.update()
        return robot.obstacles.status()

    # --- WebSocket -------------------------------------------------------------------------
    @app.websocket("/ws")
    async def ws(websocket: WebSocket, t: str | None = Query(default=None, alias="token")) -> None:
        if not same(t, token):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        send_lock = asyncio.Lock()

        async def send(payload: dict) -> None:
            async with send_lock:
                await websocket.send_json(payload)

        async def pusher() -> None:
            while True:
                await send({"type": "state", **robot.status()})
                await asyncio.sleep(0.5)

        task = asyncio.create_task(pusher())
        vtask = unsub_vision = None
        if robot.vision is not None:
            vq: asyncio.Queue = asyncio.Queue(maxsize=1)
            loop = asyncio.get_running_loop()

            async def vision_pusher() -> None:       # detecciones a la velocidad del detector, no cada 0,5 s
                while True:
                    await send(await vq.get())

            unsub_vision = robot.vision.subscribe(lambda r: loop.call_soon_threadsafe(_put_latest, vq, r))
            vtask = asyncio.create_task(vision_pusher())
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                    if not isinstance(msg, dict):
                        raise ValueError("el mensaje debe ser un objeto JSON")
                except ValueError as e:
                    await send({"type": "error", "error": f"JSON inválido: {e}"})
                    continue
                reply = handle(msg)
                if "id" in msg:
                    reply["id"] = msg["id"]
                await send(reply)
        except WebSocketDisconnect:
            pass
        finally:
            task.cancel()
            if vtask is not None:
                vtask.cancel()
                unsub_vision()
            robot.drive.stop()      # un cliente que se va no deja el robot andando (además del watchdog)

    # --- interfaz estática (sin secretos: la API exige token) --------------------------------
    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
