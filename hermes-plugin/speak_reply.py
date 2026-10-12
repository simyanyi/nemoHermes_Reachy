"""Queue one spoken Telegram reply through the installed Reachy bridge plugin."""

import json
import sys
import time
import uuid

from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from importlib import import_module

bridge = import_module("__init__")


def main() -> int:
    text = sys.stdin.read().strip()
    if not text or len(text) > 600:
        print("ERROR: reply must contain 1 to 600 characters")
        return 2
    event_id = "telegram-audio-" + uuid.uuid4().hex
    result = json.loads(bridge.report({
        "event_id": event_id,
        "severity": "healthy",
        "source": "telegram-reachy-audio",
        "summary": text,
        "speak": True,
        "gesture": True,
    }))
    if result.get("error") or result.get("state") not in {"queued", "running", "completed"}:
        print("ERROR: Reachy did not accept the spoken reply")
        return 1
    for _ in range(40):
        status = json.loads(bridge.event_status({"event_id": event_id}))
        state = status.get("state")
        if state == "completed":
            print("OK: Reachy accepted the spoken reply")
            return 0
        if state in {"failed", "expired", "cancelled"} or status.get("error"):
            print("ERROR: Reachy speech failed")
            return 1
        time.sleep(0.5)
    print("ERROR: Reachy speech confirmation timed out")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
