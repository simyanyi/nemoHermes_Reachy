"""Reachy daemon 1.9 HTTP adapter. No SDK, GPU, WebRTC or discovery inside OpenShell."""

import asyncio
import math
from uuid import uuid4

import httpx

from .config import Settings
from .models import Alert
from .tts import Speech

# Small, fixed poses: (roll degrees, pitch degrees, yaw degrees, antenna degrees).
# Return to neutral after each expression. No arbitrary agent-controlled joint targets.
POSES = {
    "healthy": [(0, -8, 0, 20), (0, 6, 0, 20)],
    "warning": [(10, 0, 0, 30)],
    "critical": [(0, 0, -12, 40), (0, 0, 12, 40)],
    "recovering": [(0, -6, 0, 15)],
    "unknown": [(-8, 0, 0, 10)],
}
REQUIRED = {
    "/api/daemon/status": "get",
    "/api/move/goto": "post",
    "/api/move/stop": "post",
    "/api/media/play_sound": "post",
    "/api/media/stop_sound": "post",
    "/api/media/sounds/upload": "post",
    "/api/media/status": "get",
    "/api/motors/status": "get",
    "/api/media/sounds/{filename}": "delete",
}


class Robot:
    def __init__(self, settings: Settings, speech: Speech, client=None):
        self.settings = settings
        self.speech = speech
        # Robot LAN traffic must not accidentally go through host HTTP_PROXY.
        self.client = client or httpx.AsyncClient(
            base_url=settings.robot_url,
            timeout=8,
            trust_env=False,
            follow_redirects=False,
        )
        self.move_id: str | None = None
        self.sound_active = False

    async def close(self):
        await self.client.aclose()

    async def request(self, method, path, **kwargs):
        response = await self.client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    async def status(self):
        if self.settings.dry_run:
            return {
                "reachable": True,
                "ready": True,
                "simulated": True,
                "speech": self.speech.readiness(),
            }
        try:
            status = await self.request("GET", "/api/daemon/status")
            media = await self.request("GET", "/api/media/status")
            backend = status.get("backend_status") or {}
            # Firmware 1.9 can leave the summary's ready flag false. This route
            # uses get_backend(), which returns HTTP 503 unless backend.ready is set.
            try:
                motors = await self.request("GET", "/api/motors/status")
                backend_ready = motors.get("mode") in {
                    "enabled",
                    "disabled",
                    "gravity_compensation",
                }
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 503:
                    raise
                motors, backend_ready = {}, False
            return {
                "reachable": True,
                "ready": status.get("state") == "running" and backend_ready,
                "daemon_state": status.get("state"),
                "backend_ready": backend_ready,
                "backend_summary_ready": backend.get("ready"),
                "readiness_source": "/api/motors/status (backend-gated)",
                "motor_mode": motors.get("mode", backend.get("motor_control_mode")),
                "media_available": media.get("available") is True,
                "firmware": status.get("version"),
                "simulated": False,
                "speech": self.speech.readiness(),
            }
        except (httpx.HTTPError, ValueError):
            return {
                "reachable": False,
                "ready": False,
                "simulated": False,
                "detail": "Cannot read robot status; check host DNS, robot URL and daemon",
                "speech": self.speech.readiness(),
            }

    async def preflight(self):
        result = await self.status()
        if self.settings.dry_run:
            return {**result, "api_compatible": True}
        try:
            schema = await self.request("GET", "/openapi.json")
            paths = schema.get("paths", {})
            missing = [p for p, verb in REQUIRED.items() if verb not in paths.get(p, {})]
            result.update(api_compatible=not missing, missing_endpoints=missing)
        except (httpx.HTTPError, ValueError):
            result.update(api_compatible=False, detail="Cannot read robot OpenAPI schema")
        return result

    async def pose(self, roll=0, pitch=0, yaw=0, antenna=0):
        result = await self.request(
            "POST",
            "/api/move/goto",
            json={
                "head_pose": {
                    "roll": math.radians(roll),
                    "pitch": math.radians(pitch),
                    "yaw": math.radians(yaw),
                },
                "antennas": [math.radians(antenna), math.radians(-antenna)],
                "duration": 0.65,
                "interpolation": "minjerk",
            },
        )
        self.move_id = result["uuid"]
        # The daemon returns a task ID, not a completion acknowledgement.
        await asyncio.sleep(0.8)
        self.move_id = None

    async def perform(self, alert: Alert):
        if self.settings.dry_run:
            return {
                "delivery": "simulated",
                "severity": alert.severity,
                "speech_requested": alert.speak,
                "gesture_requested": alert.gesture,
            }
        status = await self.status()
        if not status["ready"]:
            raise RuntimeError("Robot backend is not ready; enable/wake it in the Reachy dashboard")
        if alert.gesture and status.get("motor_mode") != "enabled":
            raise RuntimeError(
                "Robot motors are not enabled; enable/wake them in the Reachy dashboard"
            )
        if alert.speak and not status.get("media_available"):
            raise RuntimeError("Robot media is unavailable; enable media in the Reachy dashboard")
        # Synthesize first, so failed speech generation does not cause a partial gesture.
        audio = await self.speech.render(alert.summary) if alert.speak else None
        filename = "nemohermes-" + uuid4().hex + ".wav"
        uploaded = False
        try:
            if audio:
                uploaded = True
                await self.request(
                    "POST",
                    "/api/media/sounds/upload",
                    files={"file": (filename, audio[0], "audio/wav")},
                )
            if alert.gesture:
                for roll, pitch, yaw, antenna in POSES[alert.severity]:
                    await self.pose(roll, pitch, yaw, antenna)
                await self.pose()
            if audio:
                # Set before awaiting: cancellation may race the remote acknowledgement.
                self.sound_active = True
                await self.request("POST", "/api/media/play_sound", json={"file": filename})
                await asyncio.sleep(audio[1] + 0.5)
                self.sound_active = False
            return {
                "delivery": "daemon_accepted",
                "gesture_requested": alert.gesture,
                "speech_requested": alert.speak,
                "note": "Daemon accepted commands; physical motion/audio is not sensor-verified",
            }
        finally:
            if uploaded:
                # Stop before deleting on cancellation; only remove this bridge's own upload.
                if self.sound_active:
                    try:
                        await self.request("POST", "/api/media/stop_sound")
                        self.sound_active = False
                    except httpx.HTTPError:
                        pass
                try:
                    await self.request("DELETE", "/api/media/sounds/" + filename)
                except httpx.HTTPError:
                    pass

    async def stop(self):
        if self.settings.dry_run:
            return {"simulated": True}
        errors = []
        if self.move_id:
            try:
                await self.request("POST", "/api/move/stop", json={"uuid": self.move_id})
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    errors.append("Could not stop bridge movement")
            except httpx.HTTPError:
                errors.append("Could not reach robot to stop movement")
            self.move_id = None
        if self.sound_active:
            try:
                await self.request("POST", "/api/media/stop_sound")
                self.sound_active = False
            except httpx.HTTPError:
                errors.append("Could not stop robot audio")
        return {"errors": errors, "note": "Stops bridge output; does not disable motor torque"}
