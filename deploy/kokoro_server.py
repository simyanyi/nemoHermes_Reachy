"""Local WAV API using the already-cached photo-booth Kokoro image."""

import io
import json
import logging
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import torch
from kokoro import KPipeline

logging.basicConfig(level=logging.INFO)
torch.set_num_threads(2)
# The cached image has Kokoro's lexicon and voices but no neural OOV fallback
# model or phonemizer package. Unknown words are skipped rather than downloaded.
pipeline = KPipeline(lang_code="a", device="cpu", repo_id="hexgrad/Kokoro-82M",
                     fallback=lambda token: (None, None))
pipeline.g2p.lexicon.golds["Reachy"] = "riːtʃi"
voice_root = Path("/home/app/.cache/huggingface/hub/models--hexgrad--Kokoro-82M")
voices = {}
for name in ("af_bella", "af_heart", "af_nova", "af_sky"):
    matches = sorted(voice_root.glob(f"snapshots/*/voices/{name}.pt"))
    if matches:
        voices[name] = torch.load(matches[-1], map_location="cpu", weights_only=True)
if "af_bella" not in voices:
    raise RuntimeError("The cached image does not contain the Bella voice")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        logging.info(fmt, *args)

    def reply(self, code, body, content_type="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path != "/healthz":
            return self.reply(404, b'{"error":"not found"}')
        self.reply(
            200,
            json.dumps(
                {"ready": True, "model": "kokoro", "voices": list(voices), "device": "cpu"}
            ).encode(),
        )

    def do_POST(self):
        if self.path != "/v1/audio/speech":
            return self.reply(404, b'{"error":"not found"}')
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 8192:
                raise ValueError("Invalid request size")
            self.connection.settimeout(10)
            payload = json.loads(self.rfile.read(size))
            text = payload.get("input")
            voice = payload.get("voice", "af_bella")
            if not isinstance(text, str) or not 0 < len(text.strip()) <= 600:
                raise ValueError("Expected 1-600 characters")
            if voice not in voices or payload.get("response_format", "wav") != "wav":
                raise ValueError("Unsupported voice or response format")
            if payload.get("model", "kokoro") != "kokoro":
                raise ValueError("Unsupported model")
        except (ValueError, TypeError, AttributeError):
            return self.reply(422, b'{"error":"invalid speech request"}')
        try:
            with torch.inference_mode():
                chunks = [audio for _, _, audio in pipeline(text, voice=voices[voice], speed=1.05)]
                if not chunks:
                    raise RuntimeError("No speech generated")
                samples = torch.cat(chunks).clamp(-1, 1)
                if samples.numel() > 24000 * 60:
                    raise RuntimeError("Speech exceeds one minute")
                pcm = (samples * 32767).to(torch.int16).numpy().tobytes()
            output = io.BytesIO()
            with wave.open(output, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(24000)
                wav.writeframes(pcm)
            self.reply(200, output.getvalue(), "audio/wav")
        except Exception as exc:
            logging.error("Speech generation failed: %s", type(exc).__name__)
            self.reply(500, b'{"error":"speech generation failed"}')


logging.info("Kokoro ready with voices: %s", ", ".join(voices))
HTTPServer(("0.0.0.0", 8880), Handler).serve_forever()
