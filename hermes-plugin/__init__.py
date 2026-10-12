"""Hermes-native tools. Standard library only; robot credentials stay on the host."""

import json
import hashlib
import logging
import re
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


logger = logging.getLogger(__name__)
_audio_turns = set()
_audio_turns_lock = threading.Lock()


def _recent_audio_matches(user_message):
    """Match Hermes's quoted STT result to the newly cached Telegram clip."""
    if not isinstance(user_message, str) or not (
        user_message.startswith('"') and user_message.endswith('"')
    ):
        return False
    transcript = user_message[1:-1]
    if not transcript.strip():
        return False
    cache = Path('/sandbox/.hermes/cache/audio')
    try:
        clips = sorted(
            (p for p in cache.iterdir() if p.suffix in {'.ogg', '.mp3'}
             and time.time() - p.stat().st_mtime < 90),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )[:3]
    except OSError:
        return False
    try:
        from tools.transcription_tools import transcribe_audio
        for clip in clips:
            result = transcribe_audio(str(clip), None, 'gateway')
            if result.get('success') and result.get('transcript', '').strip() == transcript:
                return True
    except Exception:
        logger.exception('Telegram audio match failed')
    return False


def _pre_llm_call(*, session_id='', platform='', user_message='', **kwargs):
    if platform != 'telegram' or not session_id:
        return None
    if _recent_audio_matches(user_message):
        with _audio_turns_lock:
            _audio_turns.add(session_id)
        return {'context': 'This Telegram message came from an audio clip. Answer its request. Your final answer will be sent as text and spoken by Reachy automatically.'}
    return None


def _speech_chunks(answer):
    """Split text at words while respecting the bridge's 600-char limit."""
    remaining = re.sub(r'\s+', ' ', answer).strip()
    while remaining:
        if len(remaining) <= 560:
            yield remaining
            return
        split = remaining.rfind(' ', 0, 561)
        if split < 200:
            split = 560
        yield remaining[:split].strip()
        remaining = remaining[split:].strip()


def _speak_audio_reply(*, response_text='', session_id='', platform='', **kwargs):
    if platform != 'telegram' or not session_id:
        return None
    with _audio_turns_lock:
        if session_id not in _audio_turns:
            return None
        _audio_turns.remove(session_id)
    chunks = list(_speech_chunks(response_text))
    if not chunks:
        return None
    for index, chunk in enumerate(chunks):
        digest = hashlib.sha256(f'{session_id}:{response_text}:{index}'.encode()).hexdigest()[:24]
        result = json.loads(report({
            'event_id': 'telegram-audio-' + digest,
            'severity': 'healthy',
            'source': 'telegram-reachy-audio',
            'summary': chunk,
            'speak': True,
            'gesture': index == 0,
        }))
        if result.get('error') or result.get('state') not in {'queued', 'running', 'completed'}:
            logger.warning('Reachy rejected Telegram audio reply chunk %s', index)
            return response_text + '\n\nReachy could not speak this reply.'
    logger.info('Queued %s Reachy speech chunk(s) for Telegram audio reply', len(chunks))
    return None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(method, path, payload=None):
    try:
        config = json.loads(Path(__file__).with_name("settings.json").read_text())
        base = config["base_url"].rstrip("/")
        url = urlsplit(base)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path
        ):
            raise ValueError("base_url must be an HTTP(S) origin without credentials")
        headers = {"Content-Type": "application/json"}
        if config.get("auth_mode", "token_file") == "token_file":
            token = Path(config["token_file"]).read_text().strip()
            if len(token) < 32:
                raise ValueError("Bridge token is missing or too short")
            headers["Authorization"] = "Bearer " + token
        elif config["auth_mode"] != "openshell":
            raise ValueError("auth_mode must be token_file or openshell")
        # In openshell mode the operator must configure provider header injection separately.
        req = Request(
            base + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers=headers,
            method=method,
        )
        with build_opener(NoRedirect).open(
            req, timeout=float(config.get("timeout", 10))
        ) as response:
            return json.dumps(json.load(response))
    except HTTPError as exc:
        # Avoid raw proxy responses which could contain request headers or credentials.
        hints = {
            401: "Bridge token rejected",
            403: "Check OpenShell network policy",
            404: "Unknown event or incorrect bridge URL",
            409: "Event ID conflict or bridge stopping",
            422: "Invalid alert fields",
            429: "Queue full; wait and check event status",
        }
        return json.dumps(
            {"error": hints.get(exc.code, "Bridge HTTP error"), "http_status": exc.code}
        )
    except (URLError, TimeoutError, OSError, ValueError, KeyError):
        return json.dumps(
            {
                "error": "Bridge unavailable or misconfigured. Check settings.json, token file, "
                "host service, and OpenShell host/port/binary policy. "
                "For a timed-out report, query its event_id before retrying."
            }
        )


def status(args, **kwargs):
    return request("GET", "/v1/status")


def report(args, **kwargs):
    return request("POST", "/v1/events", args)


def event_status(args, **kwargs):
    event_id = args.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        return json.dumps({"error": "event_id is required"})
    return request("GET", "/v1/events/" + quote(event_id, safe=""))


def stop(args, **kwargs):
    return request("POST", "/v1/stop", {})


def schema(name, description, properties=None, required=None):
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties or {},
            "required": required or [],
            "additionalProperties": False,
        },
    }


TOOLS = [
    (
        schema(
            "reachy_status",
            "Read robot/backend readiness, bridge queue, and current Kubernetes or Docker "
            "telemetry statuses and failure findings. Does not move the robot.",
        ),
        status,
    ),
    (
        schema(
            "reachy_report",
            "Queue Reachy speech and a small gesture. For source reachy-voice-start, an accepted "
            "call activates listening immediately; do not check telemetry or poll event status. "
            "For infrastructure reports, use observed telemetry and check reachy_event_status "
            "before claiming delivery. Use the SAME event_id and payload for retries.",
            {
                "event_id": {
                    "type": "string",
                    "maxLength": 96,
                    "pattern": "^[A-Za-z0-9_.:-]+$",
                    "description": "Stable ID for this incident and state, e.g. checkout-503-critical-1",
                },
                "severity": {
                    "type": "string",
                    "enum": ["healthy", "warning", "critical", "recovering", "unknown"],
                },
                "summary": {
                    "type": "string",
                    "maxLength": 600,
                    "description": "One to three spoken sentences: evidence, impact, next check. No secrets.",
                },
                "source": {
                    "type": "string",
                    "maxLength": 120,
                    "description": "Event source, such as reachy-voice-start or reachy-telemetry/kubernetes",
                },
                "speak": {"type": "boolean", "default": True},
                "gesture": {"type": "boolean", "default": True},
            },
            ["event_id", "severity", "summary"],
        ),
        report,
    ),
    (
        schema(
            "reachy_event_status",
            "Read queued/running/completed/failed/cancelled/expired output. "
            "Completed means commands were accepted, not sensor-verified audibility or movement.",
            {"event_id": {"type": "string"}},
            ["event_id"],
        ),
        event_status,
    ),
    (
        schema(
            "reachy_stop",
            "Cancel this bridge's queued and active output. Does not disable motors "
            "and is not a hardware emergency stop.",
        ),
        stop,
    ),
]


def register(ctx):
    ctx.register_hook('pre_llm_call', _pre_llm_call)
    ctx.register_hook('transform_llm_output', _speak_audio_reply)
    for definition, handler in TOOLS:
        ctx.register_tool(
            name=definition["name"],
            toolset="reachy",
            schema=definition,
            handler=handler,
            check_fn=lambda: True,
            emoji="🤖",
        )
