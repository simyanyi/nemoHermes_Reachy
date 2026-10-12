---
name: stop-reachy
category: communication
description: Stop Reachy's local spoken conversation while Telegram chat keeps working.
version: 0.1.0
---

# Stop Reachy voice

When invoked from Telegram with `/stop-reachy` (or `/stop_reachy`), call
`reachy_report` with a new unique `event_id`, `source: reachy-voice-stop`,
`severity: healthy`, `summary: Reachy voice is off.`, `speak: true`, and
`gesture: false`. If the tool returns an error, explain that voice mode could
not be changed. Otherwise check the event with `reachy_event_status` and
confirm in Telegram that voice mode is off. Telegram conversation remains available.
