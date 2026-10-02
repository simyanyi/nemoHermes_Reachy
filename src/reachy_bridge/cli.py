import argparse
import asyncio
import ipaddress
import json
import os
import secrets
from pathlib import Path

import httpx
import uvicorn
import yaml

from .config import Settings
from .robot import Robot
from .tts import Speech


def policy_fragment(host: str, ips: list[str]):
    # Explicit private IPs are required by OpenShell's SSRF guard.
    networks = [str(ipaddress.ip_network(ip, strict=False)) for ip in ips]
    return {
        "network_policies": {
            "reachy_output": {
                "endpoints": [
                    {
                        "host": host,
                        "port": 8787,
                        "protocol": "rest",
                        "enforcement": "enforce",
                        "allowed_ips": networks,
                        "rules": [
                            {"allow": {"method": "GET", "path": "/healthz"}},
                            {"allow": {"method": "GET", "path": "/v1/status"}},
                            {"allow": {"method": "GET", "path": "/v1/events/*"}},
                            {"allow": {"method": "POST", "path": "/v1/events"}},
                            {"allow": {"method": "POST", "path": "/v1/stop"}},
                        ],
                    }
                ],
                "binaries": [
                    {"path": "/opt/hermes/.venv/bin/python"},
                    {"path": "/opt/hermes/.venv/bin/python3"},
                    {"path": "/usr/bin/python3*"},
                ],
            }
        }
    }


async def preflight(settings):
    robot = Robot(settings, Speech(settings))
    try:
        result = await robot.preflight()
        print(json.dumps(result, indent=2))
        return (
            0
            if (
                result["ready"]
                and result["api_compatible"]
                and (settings.dry_run or result["speech"]["configured"] or settings.tts == "none")
            )
            else 1
        )
    finally:
        await robot.close()


def main():
    parser = argparse.ArgumentParser(description="NemoHermes → host bridge → wireless Reachy")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Create a dedicated local bridge token (never overwrites one)")
    serve = sub.add_parser("serve", help="Start one bridge worker; does not enable robot motors")
    serve.add_argument("--dry-run", action="store_true")
    sub.add_parser("preflight", help="Read robot readiness and firmware API; no motion or audio")
    policy = sub.add_parser("policy", help="Print policy fragment, or merge into an exported base")
    policy.add_argument("--host", default="host.openshell.internal")
    policy.add_argument(
        "--allowed-ip",
        action="append",
        required=True,
        help="Actual bridge address as resolved inside OpenShell; repeat if needed",
    )
    policy.add_argument(
        "--base", type=Path, help="Existing base policy YAML to merge without replacing other rules"
    )
    client = sub.add_parser("client", help="Call bridge for rehearsals; report/stop affect output")
    client.add_argument("action", choices=["status", "report", "event", "stop"])
    client.add_argument("--url", default=None)
    client.add_argument("--file", type=Path, help="JSON alert payload")
    client.add_argument("--event-id")
    args = parser.parse_args()
    settings = Settings()
    if args.command == "init":
        path = settings.token_file
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            settings.token()
            print(f"Existing token retained at {path}")
        else:
            with os.fdopen(fd, "w") as output:
                output.write(secrets.token_urlsafe(32) + "\n")
            print(f"Created token at {path}; contents are not printed")
    elif args.command == "serve":
        from .service import create_app

        if args.dry_run:
            settings.dry_run = True
        uvicorn.run(
            create_app(settings),
            host=settings.bind,
            port=settings.port,
            log_level="info",
            access_log=False,
        )
    elif args.command == "preflight":
        raise SystemExit(asyncio.run(preflight(settings)))
    elif args.command == "policy":
        fragment = policy_fragment(args.host, args.allowed_ip)
        if args.base:
            raw = args.base.read_text()
            if "[STRIPPED_BY_MIGRATION]" in raw:
                parser.error(
                    "Base policy contains stripped credentials; restore supported bindings before merging"
                )
            base = yaml.safe_load(raw)
            if not isinstance(base, dict) or "version" not in base:
                parser.error("Expected a complete exported base policy with a version field")
            policies = base.setdefault("network_policies", {})
            if "reachy_output" in policies:
                parser.error(
                    "Base already contains reachy_output; review the existing rule explicitly"
                )
            policies.update(fragment["network_policies"])
            fragment = base
        print(yaml.safe_dump(fragment, sort_keys=False), end="")
    elif args.command == "client":
        from urllib.parse import quote

        method, path, payload = "GET", "/v1/status", None
        if args.action == "report":
            if not args.file:
                parser.error("report requires --file")
            method, path, payload = "POST", "/v1/events", json.loads(args.file.read_text())
        elif args.action == "event":
            if not args.event_id:
                parser.error("event requires --event-id")
            path = "/v1/events/" + quote(args.event_id, safe="")
        elif args.action == "stop":
            method, path, payload = "POST", "/v1/stop", {}
        bridge_url = args.url or f"http://{settings.bind if settings.bind != '0.0.0.0' else '127.0.0.1'}:{settings.port}"
        with httpx.Client(timeout=10, trust_env=False, follow_redirects=False) as session:
            response = session.request(
                method,
                bridge_url.rstrip("/") + path,
                json=payload,
                headers={"Authorization": "Bearer " + settings.token()},
            )
            response.raise_for_status()
            print(json.dumps(response.json(), indent=2))


if __name__ == "__main__":
    main()
