---
name: start-reachy
category: communication
description: Start a local spoken conversation with Reachy while Telegram chat remains available.
version: 0.1.0
---

# Start Reachy voice

This skill is invoked from Telegram with `/start-reachy` (or `/start_reachy`).
Go straight into the voice conversation pipeline. The passive telemetry monitor
runs separately with NemoHermes. Call `reachy_report` immediately with:

- `source`: `reachy-voice-start`
- `severity`: `healthy`
- `summary`: `Hi! I'm listening.`
- `speak`: `true`
- `gesture`: `true`
- a new, unique `event_id` for this activation; reuse it only for a retry

If `reachy_report` returns an error, explain the failure in Telegram and do not
claim voice mode is on. An accepted `reachy_report` activates listening at once.
Reply without a status or event polling call:
"Reachy voice is on. Speak to the robot nearby; Telegram still works here. Use
/stop-reachy when finished."

Voice uses the same Hermes agent, tools, and skills, with its own conversation
history. Telegram retains its chat history and continues replying to typed messages.
