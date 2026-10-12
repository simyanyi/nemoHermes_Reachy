import asyncio

import httpx
import pytest

from reachy_bridge.config import Settings
from reachy_bridge.service import create_app


class FakeRobot:
    def __init__(self, blocking=False, fail=False):
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.blocking = blocking
        self.fail = fail
        self.stops = 0

    async def status(self):
        return {"ready": True}

    async def perform(self, alert):
        self.calls.append(alert.event_id)
        self.started.set()
        if self.blocking:
            await self.release.wait()
        if self.fail:
            raise RuntimeError("Robot backend is not ready")
        return {"delivery": "test"}

    async def stop(self):
        self.stops += 1
        return {"errors": []}

    async def close(self):
        pass


@pytest.fixture
def settings(tmp_path):
    token = tmp_path / "token"
    token.write_text("a" * 40)
    return Settings(_env_file=None, token_file=token, queue_size=1)


def alert(event_id="incident-1", **kwargs):
    return {
        "event_id": event_id,
        "severity": "critical",
        "summary": "Checkout is unavailable.",
        **kwargs,
    }


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}])
async def test_unauthorized_requests_cannot_move_robot(settings, headers):
    robot = FakeRobot()
    app = create_app(settings, robot)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            for path in ["/v1/events", "/v1/stop"]:
                assert (await c.post(path, json=alert(), headers=headers)).status_code == 401
            assert (await c.get("/v1/status", headers=headers)).status_code == 401
            assert (await c.get("/healthz")).status_code == 200
    assert robot.calls == []


async def test_idempotency_conflicts_queue_limit_and_cancellation(settings):
    robot = FakeRobot(blocking=True)
    app = create_app(settings, robot)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer " + settings.token()},
        ) as c:
            assert (await c.post("/v1/events", json=alert())).status_code == 202
            await asyncio.wait_for(robot.started.wait(), 1)
            assert (await c.post("/v1/events", json=alert())).status_code == 202
            assert (await c.post("/v1/events", json=alert(summary="Changed"))).status_code == 409
            assert (await c.post("/v1/events", json=alert("incident-2"))).status_code == 202
            assert (await c.post("/v1/events", json=alert("incident-3"))).status_code == 429
            assert (await c.post("/v1/stop")).status_code == 200
            await asyncio.wait_for(app.state.engine.queue.join(), 1)
            for event_id in ["incident-1", "incident-2"]:
                result = await c.get("/v1/events/" + event_id)
                assert result.json()["state"] == "cancelled"
            assert robot.calls == ["incident-1"]
            robot.blocking = False
            assert (await c.post("/v1/events", json=alert("incident-3"))).status_code == 202
            await asyncio.wait_for(app.state.engine.queue.join(), 1)
            assert (await c.get("/v1/events/incident-3")).json()["state"] == "completed"


@pytest.mark.parametrize(
    "changes",
    [
        {"event_id": "../escape"},
        {"severity": "panic"},
        {"summary": " "},
        {"summary": "x" * 601},
        {"speak": "false"},
        {"robot_url": "http://other-host"},
    ],
)
async def test_invalid_alerts_are_rejected_before_execution(settings, changes):
    robot = FakeRobot()
    app = create_app(settings, robot)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer " + settings.token()},
        ) as c:
            assert (await c.post("/v1/events", json=alert(**changes))).status_code == 422
            assert robot.calls == []


async def test_failure_is_not_reported_as_delivery_and_request_size_is_bounded(settings):
    app = create_app(settings, FakeRobot(fail=True))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer " + settings.token()},
        ) as c:
            await c.post("/v1/events", json=alert())
            await asyncio.wait_for(app.state.engine.queue.join(), 1)
            result = (await c.get("/v1/events/incident-1")).json()
            assert result["state"] == "failed"
            assert result["result"] is None
            assert (await c.post("/v1/events", content=b"x" * 8193)).status_code == 413


async def test_stale_queued_alert_expires(settings):
    robot = FakeRobot()
    app = create_app(settings, robot)
    engine = app.state.engine
    from reachy_bridge.models import Alert

    event = engine.submit(Alert(**alert()))
    engine.queue.get_nowait()
    engine.queue.task_done()
    engine.queue.put_nowait((event, -1000))
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(engine.queue.join(), 1)
    assert event.state == "expired"
    assert robot.calls == []


async def test_voice_activation_keeps_independent_bridge_state(settings):
    app = create_app(settings, FakeRobot())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
            headers={"Authorization": "Bearer " + settings.token()},
        ) as c:
            assert (await c.get("/v1/voice")).json()["active"] is False
            start = alert("voice-start-1", source="reachy-voice-start")
            assert (await c.post("/v1/events", json=start)).status_code == 202
            assert (await c.get("/v1/voice")).json()["active"] is True
            await asyncio.wait_for(app.state.engine.queue.join(), 1)
            stop = alert("voice-stop-1", source="reachy-voice-stop")
            assert (await c.post("/v1/events", json=stop)).status_code == 202
            assert (await c.get("/v1/voice")).json()["active"] is False
