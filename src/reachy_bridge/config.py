from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="REACHY_", env_file=".env", extra="ignore")

    robot_url: str = "http://reachy-mini.local:8000"
    bind: str = "127.0.0.1"
    port: int = Field(default=8787, ge=1024, le=65535)
    token_file: Path = Path(".secrets/bridge-token")
    telemetry_config_file: Path | None = None
    kubeconfig: Path | None = None
    telemetry_namespace: str | None = None
    telemetry_deployment: str | None = None
    dry_run: bool = False
    queue_size: int = Field(default=8, ge=1, le=32)
    history_size: int = Field(default=256, ge=32, le=4096)
    event_max_age: float = Field(default=120, ge=10, le=600)
    tts: Literal["espeak", "openai", "none"] = "espeak"
    voice: str = "en-us+f3"
    speech_rate: int = Field(default=175, ge=100, le=250)
    speech_pitch: int = Field(default=65, ge=0, le=99)
    speech_range: int = Field(default=75, ge=0, le=99)
    # Optional local or hosted OpenAI-compatible /audio/speech service; host only.
    tts_base_url: str = "http://127.0.0.1:8880/v1"
    tts_model: str = "kokoro"
    tts_voice: str = "af_heart"
    tts_key_file: Path | None = None
    voice_start_audio_file: Path | None = None

    @field_validator("robot_url", "tts_base_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ValueError("Use an HTTP(S) URL without credentials, query or fragment")
        return value.rstrip("/")

    def token(self) -> str:
        value = self.token_file.read_text().strip()
        if len(value) < 32 or any(c.isspace() for c in value):
            raise ValueError("Bridge token must have at least 32 characters and no whitespace")
        return value
