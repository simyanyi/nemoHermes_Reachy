import io
import wave

import httpx
import pytest

from reachy_bridge.config import Settings
from reachy_bridge.tts import Speech


def audio_bytes():
    data = io.BytesIO()
    with wave.open(data, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0" * 3200)
    return data.getvalue()


async def test_offline_speech_uses_stdin_not_shell_or_text_arguments(monkeypatch):
    observed = {}

    async def create(*args, **kwargs):
        observed["args"] = args

        class Process:
            returncode = 0

            async def communicate(self, input):
                observed["stdin"] = input
                return audio_bytes(), None

        return Process()

    monkeypatch.setattr("reachy_bridge.tts.asyncio.create_subprocess_exec", create)
    text = "--help; $(do-not-execute)"
    data, duration = await Speech(Settings(_env_file=None)).render(text)
    assert data == audio_bytes()
    assert duration == pytest.approx(0.1)
    assert observed["stdin"] == text.encode()
    assert text not in observed["args"]


@pytest.mark.parametrize("body,valid", [(audio_bytes(), True), (b"not audio", False)])
async def test_http_tts_validates_audio_and_requests_wav(monkeypatch, body, valid):
    import json

    def handler(request):
        payload = json.loads(request.content)
        assert payload["response_format"] == "wav"
        assert payload["input"] == "Read only telemetry"
        return httpx.Response(200, content=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("reachy_bridge.tts.httpx.AsyncClient", lambda **kwargs: client)
    speech = Speech(Settings(_env_file=None, tts="openai"))
    if valid:
        assert (await speech.render("Read only telemetry"))[1] == pytest.approx(0.1)
    else:
        with pytest.raises((wave.Error, EOFError)):
            await speech.render("Read only telemetry")
