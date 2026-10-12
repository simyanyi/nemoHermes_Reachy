"""Read-only Kubernetes watchdog with edge-triggered Reachy and Telegram alerts."""

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Finding:
    key: str
    detail: str


class MonitorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["kubernetes", "docker"] = "kubernetes"
    label: str | None = None
    kubeconfig: Path | None = None
    kubectl: str = "kubectl"
    namespace: str | None = None
    deployment: str | None = None
    interval_seconds: int = Field(default=2, ge=1, le=3600)
    announce_initial: bool = True
    speak_every_check: bool = False
    gestures: bool = True
    telegram_target: str = ""

    @model_validator(mode="after")
    def check_target(self):
        if self.source == "kubernetes" and self.kubeconfig is None:
            raise ValueError("Kubernetes monitoring requires kubeconfig")
        if bool(self.namespace) != bool(self.deployment):
            raise ValueError("namespace and deployment must be supplied together")
        return self


def load_monitor_config(path: Path) -> MonitorConfig:
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid telemetry YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("Telemetry config must be a YAML mapping")
    return MonitorConfig.model_validate(raw)


def findings(pods: dict, nodes: dict, events: dict, *, now=None) -> list[Finding]:
    """Extract actionable failures from Kubernetes list responses."""
    now = now or datetime.now(UTC)
    result = []
    for node in nodes.get("items", []):
        name = node.get("metadata", {}).get("name", "unknown")
        conditions = node.get("status", {}).get("conditions", [])
        ready = next((c for c in conditions if c.get("type") == "Ready"), None)
        if ready and ready.get("status") != "True":
            result.append(Finding(f"node/{name}/not-ready", f"Node {name} is not Ready"))
    for pod in pods.get("items", []):
        meta = pod.get("metadata", {})
        name = f"{meta.get('namespace', 'default')}/{meta.get('name', 'unknown')}"
        status = pod.get("status", {})
        phase = status.get("phase")
        if phase in {"Failed", "Unknown"}:
            result.append(Finding(f"pod/{name}/phase", f"Pod {name} is {phase}"))
        for container in status.get("containerStatuses", []):
            cname = container.get("name", "unknown")
            state = container.get("state", {})
            waiting = state.get("waiting", {})
            if waiting.get("reason") in {
                "CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError"
            }:
                reason = waiting["reason"]
                result.append(Finding(f"pod/{name}/{cname}/waiting", f"{name}: {cname} is {reason}"))
            restarts = container.get("restartCount", 0)
            finished = container.get("lastState", {}).get("terminated", {}).get("finishedAt")
            if restarts and finished:
                try:
                    age = now - datetime.fromisoformat(finished.replace("Z", "+00:00"))
                    if timedelta(0) <= age <= timedelta(minutes=5):
                        result.append(Finding(
                            f"pod/{name}/{cname}/restarts",
                            f"{name}: {cname} restarted recently ({restarts} total)",
                        ))
                except ValueError:
                    pass
        # Allow startup to settle; Pending alone does not indicate failure.
        created = meta.get("creationTimestamp")
        if phase == "Pending" and created:
            try:
                age = now - datetime.fromisoformat(created.replace("Z", "+00:00"))
                if age > timedelta(minutes=5):
                    result.append(Finding(f"pod/{name}/pending", f"Pod {name} Pending for over 5 minutes"))
            except ValueError:
                pass
    for event in events.get("items", []):
        if event.get("type") != "Warning":
            continue
        meta = event.get("metadata", {})
        timestamp = event.get("lastTimestamp") or event.get("eventTime") or meta.get("creationTimestamp")
        if not timestamp:
            continue
        try:
            age = now - datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            continue
        if not timedelta(0) <= age <= timedelta(minutes=5):
            continue
        target = event.get("involvedObject", {})
        subject = f"{target.get('namespace', meta.get('namespace', 'default'))}/{target.get('kind', 'Object')}/{target.get('name', 'unknown')}"
        reason = event.get("reason", "Warning")
        result.append(Finding(f"event/{meta.get('uid', subject + reason)}", f"{subject}: {reason}"))
    return sorted(result, key=lambda item: item.key)


class KubernetesSource:
    def __init__(self, kubeconfig: Path, kubectl="kubectl", timeout=5,
                 namespace=None, deployment=None):
        self.kubeconfig = kubeconfig
        self.kubectl = kubectl
        self.timeout = timeout
        self.namespace = namespace
        self.deployment = deployment
        self.status_summary = None
        self.status_signature = None

    def read(self, kind):
        proc = subprocess.run(
            [self.kubectl, "--kubeconfig", str(self.kubeconfig), "get", kind,
             "--all-namespaces", "-o", "json", "--request-timeout=4s"],
            capture_output=True, text=True, timeout=self.timeout, check=False,
        )
        if proc.returncode:
            raise RuntimeError(f"kubectl could not read {kind} (exit {proc.returncode})")
        return json.loads(proc.stdout)

    def snapshot(self):
        pods = self.read("pods")
        nodes = self.read("nodes")
        events = self.read("events")
        result = findings(pods, nodes, events)
        node_items = nodes.get("items", [])
        ready_nodes = sum(
            any(c.get("type") == "Ready" and c.get("status") == "True"
                for c in node.get("status", {}).get("conditions", []))
            for node in node_items
        )
        parts = [f"{ready_nodes} of {len(node_items)} nodes Ready"]
        signature = [ready_nodes, len(node_items)]
        warning_count = sum(item.key.startswith("event/") for item in result)
        if self.namespace and self.deployment:
            deployment = next((item for item in self.read("deployments").get("items", [])
                               if item.get("metadata", {}).get("namespace") == self.namespace
                               and item.get("metadata", {}).get("name") == self.deployment), None)
            if deployment is None:
                result.append(Finding(f"deployment/{self.namespace}/{self.deployment}/missing",
                                      f"Deployment {self.namespace}/{self.deployment} is missing"))
                parts.append(f"namespace {self.namespace}, deployment {self.deployment} missing")
                signature.append(None)
            else:
                desired = deployment.get("spec", {}).get("replicas", 1)
                ready = deployment.get("status", {}).get("readyReplicas", 0)
                selector = deployment.get("spec", {}).get("selector", {}).get("matchLabels", {})
                target_pods = [pod for pod in pods.get("items", [])
                               if pod.get("metadata", {}).get("namespace") == self.namespace
                               and selector and all(pod.get("metadata", {}).get("labels", {}).get(k) == v
                                                    for k, v in selector.items())]
                ready_pods = sum(
                    any(c.get("type") == "Ready" and c.get("status") == "True"
                        for c in pod.get("status", {}).get("conditions", []))
                    for pod in target_pods
                )
                recent_restarts = sum(
                    item.key.startswith(f"pod/{self.namespace}/") and item.key.endswith("/restarts")
                    for item in result
                )
                parts.append(
                    f"namespace {self.namespace}, deployment {self.deployment}: "
                    f"{ready} of {desired} replicas Ready"
                )
                parts.append(f"{ready_pods} of {len(target_pods)} pods Ready")
                parts.append(f"{recent_restarts} recent pod restarts")
                signature.extend((ready, desired, ready_pods, len(target_pods), recent_restarts))
                if ready < desired:
                    result.append(Finding(
                        f"deployment/{self.namespace}/{self.deployment}/unavailable",
                        f"Deployment {self.namespace}/{self.deployment}: {ready} of {desired} replicas Ready",
                    ))
        parts.append(f"{warning_count} recent warning events")
        signature.append(warning_count)
        self.status_summary = "; ".join(parts)
        self.status_signature = tuple(signature)
        return sorted(result, key=lambda item: item.key)


class DockerSource:
    """Inspect all current containers and recent error lines without Docker write access."""

    error_pattern = re.compile(r"\b(error|fatal|panic|traceback)\b", re.IGNORECASE)

    def __init__(self, docker="docker", timeout=5):
        self.docker = docker
        self.timeout = timeout
        self.seen_logs = {}
        self.last_restarts = {}
        self.recent_restarts = {}
        self.last_inventory = []
        self.status_summary = None
        self.status_signature = None

    def _run(self, *args):
        proc = subprocess.run(
            [self.docker, *args], capture_output=True, text=True,
            timeout=self.timeout, check=False,
        )
        if proc.returncode:
            raise RuntimeError(f"Docker telemetry read failed (exit {proc.returncode})")
        return proc.stdout + proc.stderr if args[0] == "logs" else proc.stdout

    def snapshot(self):
        ids = self._run("ps", "-aq").splitlines()
        if not ids:
            self.last_inventory = []
            self.status_summary = "0 containers running"
            self.status_signature = (0, 0)
            return [Finding("docker/no-containers", "No Docker containers are present")]
        data = json.loads(self._run("inspect", *ids))
        now = datetime.now(UTC).timestamp()
        result = []
        inventory = []
        for item in data:
            name = item.get("Name", "unknown").lstrip("/")
            state = item.get("State", {})
            status = state.get("Status", "unknown")
            health = state.get("Health", {}).get("Status")
            restart_count = item.get("RestartCount", 0)
            inventory.append({"name": name, "status": status, "health": health,
                              "restart_count": restart_count})
            previous = self.last_restarts.get(item["Id"])
            if previous is not None and restart_count > previous:
                self.recent_restarts[name] = (now, restart_count - previous)
            self.last_restarts[item["Id"]] = restart_count
            if status != "running":
                reason = "OOM killed" if state.get("OOMKilled") else f"{status}, exit {state.get('ExitCode', '?')}"
                result.append(Finding(f"container/{name}/state", f"Container {name}: {reason}"))
            if health == "unhealthy":
                result.append(Finding(f"container/{name}/health", f"Container {name} is unhealthy"))
            recent_restart = self.recent_restarts.get(name)
            if recent_restart and now - recent_restart[0] <= 300:
                result.append(Finding(f"container/{name}/restart",
                                      f"Container {name} restarted {recent_restart[1]} time(s)"))
            # Only collect new errors; never forward raw log contents or secrets.
            try:
                logs = self._run("logs", "--since", "12s", "--tail", "200", item["Id"])
            except RuntimeError:
                continue
            for line in logs.splitlines():
                if not self.error_pattern.search(line):
                    continue
                normalized = re.sub(r"^\S+\s+", "", line).strip()
                digest = hashlib.sha256(normalized.encode()).hexdigest()[:16]
                key = f"container/{name}/log/{digest}"
                if now - self.seen_logs.get(key, 0) > 300:
                    self.seen_logs[key] = now
            result.extend(
                Finding(key, f"Container {name} emitted an error log")
                for key, seen_at in self.seen_logs.items()
                if key.startswith(f"container/{name}/log/") and now - seen_at <= 300
            )
        self.seen_logs = {key: at for key, at in self.seen_logs.items() if now - at <= 300}
        self.recent_restarts = {key: item for key, item in self.recent_restarts.items()
                                if now - item[0] <= 300}
        self.last_restarts = {key: value for key, value in self.last_restarts.items()
                              if key in ids}
        self.last_inventory = sorted(inventory, key=lambda item: item["name"])
        running = sum(item["status"] == "running" for item in inventory)
        self.status_summary = f"{running} of {len(inventory)} containers running"
        self.status_signature = (running, len(inventory))
        return sorted(result, key=lambda item: item.key)


class Reporter:
    def __init__(self, bridge: Settings, telegram_target: str, hermes_sandbox="my-hermes",
                 source="reachy-telemetry/docker", gestures=False):
        self.bridge = bridge
        self.telegram_target = telegram_target
        self.hermes_sandbox = hermes_sandbox
        self.source = source
        self.gestures = gestures

    async def deliver(self, severity: str, summary: str, event_id: str):
        errors = []
        headers = {"Authorization": "Bearer " + self.bridge.token()}
        try:
            async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                response = await client.post(
                    f"http://{self.bridge.bind if self.bridge.bind != '0.0.0.0' else '127.0.0.1'}:{self.bridge.port}/v1/events",
                    headers=headers,
                    json={"event_id": event_id, "severity": severity, "summary": summary,
                          "source": self.source, "speak": True, "gesture": self.gestures},
                )
                response.raise_for_status()
        except (httpx.HTTPError, OSError) as exc:
            errors.append(f"Reachy delivery failed: {type(exc).__name__}")
        if self.telegram_target:
            env = dict(os.environ, NEMOCLAW_GATEWAY_PORT=os.environ.get("NEMOCLAW_GATEWAY_PORT", "8080"))
            try:
                proc = await asyncio.create_subprocess_exec(
                    str(Path.home() / ".local/bin/nemohermes"), self.hermes_sandbox,
                    "exec", "--", "hermes", "send",
                    "--to", self.telegram_target, summary,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
                )
                if await asyncio.wait_for(proc.wait(), timeout=30) != 0:
                    errors.append("Telegram delivery command failed")
            except (OSError, TimeoutError) as exc:
                errors.append(f"Telegram delivery failed: {type(exc).__name__}")
        for error in errors:
            logger.error(error)
        return not errors


class Watchdog:
    def __init__(self, source, reporter, interval=10, label="Docker", speak_every_check=False,
                 announce_initial=False):
        self.source = source
        self.reporter = reporter
        self.interval = interval
        self.label = label
        self.speak_every_check = speak_every_check
        self.announce_initial = announce_initial
        self.last_state = None
        self.pending_id = None
        self.pending_signature = None

    async def tick(self):
        try:
            current = await asyncio.to_thread(self.source.snapshot)
            signature = tuple((item.key, item.detail) for item in current)
            source_status = getattr(self.source, "status_summary", None)
            source_signature = getattr(self.source, "status_signature", None)
            if source_signature is not None:
                signature += (("status", source_signature),)
            if not current and self.last_state:
                summary = (
                    f"{self.label} recovered: all monitored services are healthy"
                    + (f"; {source_status}" if source_status else ".")
                )
            else:
                summary = (
                f"{self.label} failure: " + (source_status + "; " if source_status else "")
                + "; ".join(item.detail for item in current[:3])
                + (f"; and {len(current) - 3} more findings" if len(current) > 3 else "")
                if current else (f"{self.label} healthy: {source_status}"
                                 if source_status else
                                 f"{self.label} cluster healthy; no current failures detected.")
                )
            severity = "critical" if current else ("recovering" if self.last_state else "healthy")
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            current = []
            signature = (("source/unavailable", type(exc).__name__),)
            summary = f"{self.label} telemetry unavailable; check the local read-only access."
            severity = "unknown"
        if signature == self.last_state and not self.speak_every_check:
            return
        if not signature and self.last_state is None and not (
            self.speak_every_check or self.announce_initial
        ):
            self.last_state = signature
            return
        digest = hashlib.sha256(repr(signature).encode()).hexdigest()[:16]
        if signature != self.pending_signature:
            self.pending_signature = signature
            self.pending_id = f"telemetry-{digest}-{uuid.uuid4().hex[:12]}"
        event_id = self.pending_id
        if await self.reporter.deliver(severity, summary[:600], event_id):
            self.last_state = signature
            self.pending_id = None
            self.pending_signature = None
            logger.info("%s telemetry state changed: %s findings", self.label, len(current))

    async def run(self):
        while True:
            started = asyncio.get_running_loop().time()
            await self.tick()
            await asyncio.sleep(max(0, self.interval - (asyncio.get_running_loop().time() - started)))
