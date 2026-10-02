from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Severity = Literal["healthy", "warning", "critical", "recovering", "unknown"]


def now() -> str:
    return datetime.now(UTC).isoformat()


class Alert(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    event_id: str = Field(min_length=1, max_length=96, pattern=r"^[A-Za-z0-9_.:-]+$")
    severity: Severity
    summary: str = Field(min_length=1, max_length=600)
    source: str = Field(default="hermes", min_length=1, max_length=120)
    speak: bool = True
    gesture: bool = True

    @field_validator("summary", "source")
    @classmethod
    def clean_text(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Text must not be blank")
        if any(ord(c) < 32 for c in value):
            raise ValueError("Control characters are not allowed")
        return value


class Event(BaseModel):
    alert: Alert
    state: Literal["queued", "running", "completed", "failed", "cancelled", "expired"] = "queued"
    created_at: str = Field(default_factory=now)
    finished_at: str | None = None
    detail: str | None = None
    simulated: bool = False
    result: dict | None = None
