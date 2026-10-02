import asyncio
import logging
from collections import OrderedDict
from contextlib import asynccontextmanager
from hmac import compare_digest
from time import monotonic
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings
from .models import Alert, Event, now
from .robot import Robot
from .tts import Speech

logger = logging.getLogger(__name__)


class Engine:
    def __init__(self, settings, robot):
        self.settings = settings
        self.robot = robot
        self.events = OrderedDict()
        self.queue = asyncio.Queue(maxsize=settings.queue_size)
        self.task = None
        self.active = None
        self.stopping = False
        self.stop_lock = asyncio.Lock()

    def submit(self, alert):
        if self.stopping:
            raise HTTPException(409, "Bridge is stopping; retry after status check")
        if alert.event_id in self.events:
            existing = self.events[alert.event_id]
            if existing.alert != alert:
                raise HTTPException(409, "Event ID already used with a different payload")
            return existing
        if self.queue.full():
            raise HTTPException(429, "Output queue is full; wait before submitting new events")
        while len(self.events) >= self.settings.history_size:
            removable = next(
                (
                    key
                    for key, event in self.events.items()
                    if event.state not in {"queued", "running"}
                ),
                None,
            )
            if removable is None:
                raise HTTPException(429, "Event history is busy")
            self.events.pop(removable)
        event = Event(alert=alert, simulated=self.settings.dry_run)
        self.events[alert.event_id] = event
        self.queue.put_nowait((event, monotonic()))
        return event

    async def work(self):
        while True:
            event, queued_at = await self.queue.get()
            try:
                if monotonic() - queued_at > self.settings.event_max_age:
                    event.state = "expired"
                    event.detail = "Dropped stale telemetry output"
                    continue
                event.state = "running"
                self.active = asyncio.create_task(self.robot.perform(event.alert))
                try:
                    event.result = await self.active
                    event.state = "completed"
                except asyncio.CancelledError:
                    event.state = "cancelled"
                    if asyncio.current_task().cancelling():
                        raise
                except Exception as exc:
                    event.state = "failed"
                    # Avoid leaking upstream response bodies, URLs or credential values.
                    event.detail = (
                        str(exc)
                        if isinstance(exc, RuntimeError)
                        else (f"{type(exc).__name__}: output failed; check robot/TTS configuration")
                    )
                    logger.warning(
                        "Reachy event %s failed (%s)", event.alert.event_id, type(exc).__name__
                    )
                    await self.robot.stop()
            finally:
                event.finished_at = now()
                self.active = None
                self.queue.task_done()

    async def stop(self):
        async with self.stop_lock:
            self.stopping = True
            try:
                # Drain before the active task yields, preventing the worker starting another job.
                while not self.queue.empty():
                    event, _ = self.queue.get_nowait()
                    event.state = "cancelled"
                    event.finished_at = now()
                    self.queue.task_done()
                active = self.active
                if active and not active.done():
                    active.cancel()
                    try:
                        await active
                    except asyncio.CancelledError:
                        pass
                return await self.robot.stop()
            finally:
                self.stopping = False


def create_app(settings: Settings | None = None, robot=None):
    settings = settings or Settings()
    token = settings.token()  # Fail closed at startup; never silently run without auth.
    robot = robot or Robot(settings, Speech(settings))
    engine = Engine(settings, robot)

    @asynccontextmanager
    async def lifespan(app):
        engine.task = asyncio.create_task(engine.work())
        yield
        await engine.stop()
        engine.task.cancel()
        try:
            await engine.task
        except asyncio.CancelledError:
            pass
        await robot.close()

    app = FastAPI(
        title="NemoHermes Reachy output bridge",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.engine = engine
    bearer = HTTPBearer(auto_error=False)

    async def authorize(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ):
        if credentials is None or not compare_digest(
            credentials.credentials.encode(), token.encode()
        ):
            raise HTTPException(
                401, "Invalid bridge credential", headers={"WWW-Authenticate": "Bearer"}
            )

    @app.middleware("http")
    async def limit_body(request: Request, call_next):
        # Cap streaming bodies too, not just Content-Length. This is a small JSON API.
        from starlette.responses import JSONResponse

        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 8192:
                return JSONResponse({"detail": "Request body exceeds 8 KiB"}, status_code=413)
        request._body = bytes(body)
        return await call_next(request)

    @app.get("/healthz")
    async def health():
        return {"service": "reachy-bridge", "ok": True, "simulated": settings.dry_run}

    @app.get("/v1/status", dependencies=[Depends(authorize)])
    async def status():
        return {
            "robot": await robot.status(),
            "queued": engine.queue.qsize(),
            "active": next((key for key, e in engine.events.items() if e.state == "running"), None),
            "stopping": engine.stopping,
        }

    @app.post("/v1/events", status_code=202, dependencies=[Depends(authorize)])
    async def submit(alert: Alert):
        return engine.submit(alert)

    @app.get("/v1/events/{event_id}", dependencies=[Depends(authorize)])
    async def event(event_id: str):
        if event_id not in engine.events:
            raise HTTPException(404, "Unknown event (history is bounded and resets on restart)")
        return engine.events[event_id]

    @app.post("/v1/stop", dependencies=[Depends(authorize)])
    async def stop():
        return await engine.stop()

    return app
