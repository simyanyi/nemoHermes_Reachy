"""Host-side Reachy microphone capture and local speech recognition.

The Hermes gateway owns conversation state and tool access; this process only
turns microphone audio into text and waits while Reachy speaks the reply.
"""

import asyncio
import logging
import os
import uuid
from pathlib import Path
from time import monotonic

import httpx

from .config import Settings

logger = logging.getLogger(__name__)


class VoiceLoop:
    def __init__(self, settings: Settings, model_path: str, wake_word="hey reachy"):
        self.settings = settings
        bind = settings.bind if settings.bind != "0.0.0.0" else "127.0.0.1"
        self.base = f"http://{bind}:{settings.port}"
        self.headers = {"Authorization": "Bearer " + settings.token()}
        self.model_path = model_path
        self.wake_word = wake_word.lower().strip()

    async def run(self):
        try:
            import numpy as np
            from faster_whisper import WhisperModel
            from reachy_mini import ReachyMini
        except ImportError as exc:
            raise RuntimeError(
                "Voice mode requires reachy-mini, numpy and faster-whisper in the host environment"
            ) from exc
        model = WhisperModel(self.model_path, device="cpu", compute_type="int8", local_files_only=True)
        async with httpx.AsyncClient(timeout=25, headers=self.headers, trust_env=False) as client:
            with ReachyMini(connection_mode="network") as robot:
                logger.info("Reachy microphone connected; waiting for Telegram voice activation")
                while True:
                    bridge = await client.get(self.base + "/v1/voice")
                    bridge.raise_for_status()
                    if not bridge.json().get("active"):
                        await asyncio.sleep(1)
                        continue
                    if bridge.json().get("busy"):
                        await asyncio.sleep(0.5)
                        continue
                    audio = await self._capture(robot, np)
                    if audio is None:
                        continue
                    transcript = await asyncio.to_thread(self._transcribe, model, audio)
                    if not transcript:
                        continue
                    if self.wake_word and transcript.lower().startswith(self.wake_word):
                        transcript = transcript[len(self.wake_word):].strip(" ,.!?")
                    if not transcript:
                        continue
                    bridge = await client.get(self.base + "/v1/voice")
                    bridge.raise_for_status()
                    if not bridge.json().get("active"):
                        continue
                    logger.info("Reachy heard a voice turn")
                    reply = await self._ask_hermes(transcript)
                    if not reply:
                        logger.error("Hermes returned no voice reply")
                        continue
                    response = await client.post(self.base + "/v1/events", json={
                        "event_id": "voice-" + uuid.uuid4().hex,
                        "severity": "healthy", "summary": reply[:600],
                        "source": "reachy-voice", "speak": True, "gesture": True,
                    })
                    response.raise_for_status()
                    await self._await_reply(client, response.json()["alert"]["event_id"])

    @staticmethod
    def _transcribe(model, audio):
        segments, _ = model.transcribe(audio, language="en", vad_filter=True)
        return " ".join(segment.text.strip() for segment in segments).strip()

    async def _ask_hermes(self, transcript):
        env = dict(os.environ, NEMOCLAW_GATEWAY_PORT=os.environ.get("NEMOCLAW_GATEWAY_PORT", "8080"))
        command = [
            str(Path.home() / ".local/bin/nemohermes"), "my-hermes", "exec", "--",
            "hermes", "chat", "-Q",
            "--continue", "reachy-voice", "--create-if-missing",
            "--skills", "communication/reachy-voice", "--query", transcript,
        ]
        proc = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError("Hermes voice turn timed out") from None
        if proc.returncode:
            logger.error("Hermes voice turn failed (exit %d): %s", proc.returncode,
                         stderr.decode(errors="replace")[-300:])
            return None
        lines = stdout.decode(errors="replace").splitlines()
        # Hermes writes session metadata to stderr and the quiet final answer to stdout.
        lines = [line.strip() for line in lines
                 if line.strip() and "tirith security scanner" not in line
                 and not line.startswith("Warning: Unknown toolsets:")]
        return " ".join(lines) or None

    async def _capture(self, robot, np):
        samples = []
        speech = False
        silent_for = 0.0
        started = monotonic()
        robot.media.start_recording()
        try:
            while monotonic() - started < 30:
                chunk = await asyncio.to_thread(robot.media.get_audio_sample)
                if chunk is None:
                    await asyncio.sleep(0.01)
                    continue
                if not isinstance(chunk, np.ndarray):
                    chunk = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768
                if chunk.ndim > 1:
                    chunk = chunk.mean(axis=1)
                chunk = chunk.astype(np.float32)
                duration = len(chunk) / 16000
                energy = float(np.abs(chunk).mean())
                if energy > 0.01:
                    speech = True
                    silent_for = 0
                elif speech:
                    silent_for += duration
                if speech:
                    samples.append(chunk)
                if speech and silent_for >= 1.2:
                    break
            return np.concatenate(samples) if samples else None
        finally:
            robot.media.stop_recording()

    async def _await_reply(self, client, event_id):
        deadline = monotonic() + 180
        while monotonic() < deadline:
            response = await client.get(self.base + f"/v1/events/{event_id}")
            response.raise_for_status()
            status = response.json()
            if status.get("state") in {"completed", "failed", "expired", "cancelled"}:
                return
            await asyncio.sleep(0.5)
        logger.warning("Reachy voice turn timed out; resuming listening")
