import asyncio
import io
import math
import wave

import httpx
import pytest

from reachy_bridge.config import Settings
from reachy_bridge.models import Alert
from reachy_bridge.robot import Robot
from reachy_bridge.tts import Speech


def wav_bytes():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0" * 3200)
    return buffer.getvalue()


class FakeSpeech:
    def readiness(self):
        return {"configured": True}

    async def render(self, text):
        return wav_bytes(), 0.1


def daemon_request(request):
    if request.url.path == "/api/daemon/status":
        return httpx.Response(
            200,
            json={
                "state": "running",
                "backend_status": {"ready": True, "motor_control_mode": "enabled"},
                "version": "1.9.0",
            },
        )
    if request.url.path == "/api/media/status":
        return httpx.Response(200, json={"available": True})
    if request.url.path == "/api/motors/status":
        return httpx.Response(200, json={"mode": "enabled"})
    if request.url.path == "/api/move/goto":
        return httpx.Response(200, json={"uuid": "move-test-id"})
    return httpx.Response(200, json={"status": "ok"})


async def test_report_converts_degrees_uploads_plays_and_cleans_only_own_audio(monkeypatch):
    import json

    calls = []

    def handle(req):
        calls.append(req)
        return daemon_request(req)

    async def no_wait(_):
        pass

    monkeypatch.setattr("reachy_bridge.robot.asyncio.sleep", no_wait)
    robot = Robot(
        Settings(_env_file=None),
        FakeSpeech(),
        httpx.AsyncClient(base_url="http://robot", transport=httpx.MockTransport(handle)),
    )
    result = await robot.perform(Alert(event_id="test", severity="critical", summary="Test"))
    assert result["delivery"] == "daemon_accepted"
    poses = [json.loads(r.content) for r in calls if r.url.path == "/api/move/goto"]
    assert poses[0]["head_pose"]["yaw"] == pytest.approx(math.radians(-12))
    assert poses[-1]["head_pose"] == {"roll": 0, "pitch": 0, "yaw": 0}
    playback = next(r for r in calls if r.url.path == "/api/media/play_sound")
    name = json.loads(playback.content)["file"]
    assert name.startswith("nemohermes-") and name.endswith(".wav")
    assert calls[-1].method == "DELETE"
    assert calls[-1].url.path == "/api/media/sounds/" + name
    await robot.close()


@pytest.mark.parametrize("source", ["reachy-voice", "reachy-voice-start"])
async def test_voice_uses_small_gesture_during_playback(monkeypatch, source):
    import json

    calls = []

    def handle(req):
        calls.append(req)
        return daemon_request(req)

    async def no_wait(_):
        pass

    monkeypatch.setattr("reachy_bridge.robot.asyncio.sleep", no_wait)
    robot = Robot(
        Settings(_env_file=None), FakeSpeech(),
        httpx.AsyncClient(base_url="http://robot", transport=httpx.MockTransport(handle)),
    )
    await robot.perform(Alert(event_id="voice-1", severity="healthy", summary="Hi",
                              source=source, gesture=True))
    paths = [req.url.path for req in calls]
    assert paths.index("/api/media/play_sound") < paths.index("/api/move/goto")
    poses = [json.loads(req.content) for req in calls if req.url.path == "/api/move/goto"]
    assert poses[0]["head_pose"]["pitch"] == pytest.approx(math.radians(-4))
    assert poses[-1]["head_pose"] == {"roll": 0, "pitch": 0, "yaw": 0}
    await robot.close()


@pytest.mark.parametrize("severity,expected_roll", [("healthy", -4), ("critical", 7)])
async def test_telemetry_uses_small_status_pose(monkeypatch, severity, expected_roll):
    import json

    calls = []

    def handle(req):
        calls.append(req)
        return daemon_request(req)

    async def no_wait(_):
        pass

    monkeypatch.setattr("reachy_bridge.robot.asyncio.sleep", no_wait)
    robot = Robot(
        Settings(_env_file=None), FakeSpeech(),
        httpx.AsyncClient(base_url="http://robot", transport=httpx.MockTransport(handle)),
    )
    result = await robot.perform(Alert(event_id="telemetry-1", severity=severity,
                                       summary="Cluster status", source="reachy-telemetry/kubernetes",
                                       gesture=True))
    poses = [json.loads(req.content) for req in calls if req.url.path == "/api/move/goto"]
    assert result["gesture_performed"] is True
    assert poses[0]["head_pose"]["roll"] == pytest.approx(math.radians(expected_roll))
    assert poses[-1]["head_pose"] == {"roll": 0, "pitch": 0, "yaw": 0}
    await robot.close()


async def test_voice_start_uses_cached_greeting_without_tts(monkeypatch, tmp_path):
    class NoSpeech(FakeSpeech):
        async def render(self, text):
            raise AssertionError("Greeting should use cached audio")

    async def no_wait(_):
        pass

    monkeypatch.setattr("reachy_bridge.robot.asyncio.sleep", no_wait)
    greeting = tmp_path / "greeting.wav"
    greeting.write_bytes(wav_bytes())
    calls = []

    def handle(req):
        calls.append(req.url.path)
        return daemon_request(req)

    robot = Robot(
        Settings(_env_file=None, voice_start_audio_file=greeting), NoSpeech(),
        httpx.AsyncClient(base_url="http://robot", transport=httpx.MockTransport(handle)),
    )
    result = await robot.perform(Alert(event_id="start-1", severity="healthy",
                                       summary="Hi! I'm listening.", source="reachy-voice-start",
                                       gesture=True))
    assert result["gesture_performed"] is True
    assert "/api/media/play_sound" in calls
    await robot.close()


async def test_unready_robot_never_receives_mutations():
    calls = []

    def handle(req):
        calls.append(req)
        return httpx.Response(200, json={"state": "running", "backend_status": {"ready": False}})

    robot = Robot(
        Settings(_env_file=None),
        FakeSpeech(),
        httpx.AsyncClient(base_url="http://robot", transport=httpx.MockTransport(handle)),
    )
    with pytest.raises(RuntimeError, match="not ready"):
        await robot.perform(Alert(event_id="test", severity="warning", summary="Test"))
    assert all(r.method == "GET" for r in calls)
    await robot.close()


async def test_cancel_during_speech_stops_audio_before_deleting_upload():
    calls = []
    playing = asyncio.Event()

    def handle(req):
        calls.append(req)
        if req.url.path == "/api/media/play_sound":
            playing.set()
        return daemon_request(req)

    robot = Robot(
        Settings(_env_file=None),
        FakeSpeech(),
        httpx.AsyncClient(base_url="http://robot", transport=httpx.MockTransport(handle)),
    )
    task = asyncio.create_task(
        robot.perform(Alert(event_id="test", severity="healthy", summary="Test", gesture=False))
    )
    await asyncio.wait_for(playing.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls[-2].url.path == "/api/media/stop_sound"
    assert calls[-1].method == "DELETE"
    await robot.close()


async def test_dry_run_never_calls_robot_or_tts():
    def fail(req):
        raise AssertionError("Dry run made a network call")

    settings = Settings(_env_file=None, dry_run=True)
    robot = Robot(
        settings,
        Speech(settings),
        httpx.AsyncClient(base_url="http://robot", transport=httpx.MockTransport(fail)),
    )
    result = await robot.perform(Alert(event_id="test", severity="healthy", summary="Test"))
    assert result["delivery"] == "simulated"
    assert (await robot.preflight())["simulated"] is True
    await robot.close()
