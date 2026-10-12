import asyncio
import io
import sys
import wave

import httpx

from .config import Settings
from .espeak_library import library_name


class Speech:
    def __init__(self, settings: Settings):
        self.settings = settings

    def readiness(self) -> dict:
        s = self.settings
        if s.tts == "none":
            return {"configured": False, "backend": "none", "detail": "Speech disabled"}
        if s.tts == "espeak":
            available = library_name() is not None
            return {
                "configured": available,
                "backend": "espeak",
                "detail": "Installed speech library available"
                if available
                else "eSpeak library not found",
            }
        return {
            "configured": s.tts_key_file is None or s.tts_key_file.is_file(),
            "backend": "openai",
            "detail": "Provider not contacted by status check",
        }

    async def render(self, text: str) -> tuple[bytes, float]:
        s = self.settings
        if s.tts == "none":
            raise RuntimeError("Speech is disabled; set speak=false or configure TTS")
        if s.tts == "espeak":
            # Use existing host libraries in an isolated, cancellable Python process.
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "reachy_bridge.espeak_library",
                s.voice,
                str(s.speech_rate),
                str(s.speech_pitch),
                str(s.speech_range),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                audio, _ = await asyncio.wait_for(proc.communicate(text.encode()), timeout=30)
                if proc.returncode:
                    raise RuntimeError(
                        "Installed eSpeak library could not synthesize speech; check voice data"
                    )
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
        else:
            headers = {}
            if s.tts_key_file:
                headers["Authorization"] = "Bearer " + s.tts_key_file.read_text().strip()
            async with httpx.AsyncClient(timeout=45, follow_redirects=False) as client:
                async with client.stream(
                    "POST",
                    s.tts_base_url + "/audio/speech",
                    headers=headers,
                    json={
                        "model": s.tts_model,
                        "voice": s.tts_voice,
                        "input": text,
                        "response_format": "wav",
                    },
                ) as response:
                    response.raise_for_status()
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > 20 * 1024 * 1024:
                            raise RuntimeError("Speech response exceeds 20 MiB")
                    audio = bytes(chunks)
        return audio, wav_duration(audio)


def wav_duration(audio: bytes) -> float:
    """Validate PCM WAV and return playback length."""
    # Require real PCM WAV; never upload arbitrary provider response bodies.
    with wave.open(io.BytesIO(audio), "rb") as wav:
        duration = wav.getnframes() / wav.getframerate()
        if not 0 < duration <= 60 or wav.getcomptype() != "NONE":
            raise RuntimeError("Speech must be PCM WAV lasting at most 60 seconds")
        if len(wav.readframes(wav.getnframes())) != (
            wav.getnframes() * wav.getnchannels() * wav.getsampwidth()
        ):
            raise RuntimeError("Speech WAV is truncated")
    return duration
