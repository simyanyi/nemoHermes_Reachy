"""Hermes-native tools. Standard library only; robot credentials stay on the host."""

import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


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
            "Read robot/backend readiness and bridge queue. Does not move the robot.",
        ),
        status,
    ),
    (
        schema(
            "reachy_report",
            "Queue a short spoken infrastructure report and a fixed severity gesture. "
            "Use observed telemetry; never invent data or claim remediation. Use the SAME event_id "
            "and payload for retries, a NEW ID for a changed incident state. A queued response is not "
            "delivery: check reachy_event_status. A simulated result means no hardware output.",
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
                    "description": "Telemetry source/cluster/namespace, not a credential",
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
    for definition, handler in TOOLS:
        ctx.register_tool(
            name=definition["name"],
            toolset="reachy",
            schema=definition,
            handler=handler,
            check_fn=lambda: True,
            emoji="🤖",
        )
