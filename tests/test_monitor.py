import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from reachy_bridge.monitor import (
    DockerSource,
    Finding,
    KubernetesSource,
    Watchdog,
    findings,
    load_monitor_config,
)


def test_findings_detect_failures_and_ignore_old_events():
    now = datetime.now(UTC)
    pods = {"items": [{"metadata": {"namespace": "app", "name": "api"},
                       "status": {"phase": "Running", "containerStatuses": [
                           {"name": "api", "restartCount": 2,
                            "lastState": {"terminated": {"finishedAt": now.isoformat()}},
                            "state": {"waiting": {"reason": "CrashLoopBackOff"}}}]}}]}
    nodes = {"items": [{"metadata": {"name": "worker"}, "status": {"conditions": [
        {"type": "Ready", "status": "False"}]}}]}
    events = {"items": [{"metadata": {"uid": "recent"}, "type": "Warning",
                         "lastTimestamp": now.isoformat(), "reason": "BackOff",
                         "involvedObject": {"namespace": "app", "kind": "Pod", "name": "api"}},
                        {"metadata": {"uid": "old"}, "type": "Warning",
                         "lastTimestamp": (now - timedelta(hours=1)).isoformat(),
                         "reason": "Old"}]}
    result = findings(pods, nodes, events, now=now)
    assert len(result) == 4
    assert not any("Old" in item.detail for item in result)


def test_findings_ignore_old_restart_counts():
    now = datetime.now(UTC)
    pods = {"items": [{"metadata": {"namespace": "kube-system", "name": "coredns"},
                       "status": {"phase": "Running", "containerStatuses": [{
                           "name": "coredns", "restartCount": 1,
                           "lastState": {"terminated": {"finishedAt":
                               (now - timedelta(hours=1)).isoformat()}},
                       }]}}]}
    assert findings(pods, {"items": []}, {"items": []}, now=now) == []


def test_kubernetes_source_reports_deployment_ready_count():
    source = KubernetesSource(None, namespace="reachy-demo", deployment="hello")
    state = {"ready": 2}

    def read(kind):
        if kind == "nodes":
            return {"items": [{"status": {"conditions": [{"type": "Ready", "status": "True"}]}}]}
        if kind == "deployments":
            return {"items": [{"metadata": {"namespace": "reachy-demo", "name": "hello"},
                              "spec": {"replicas": 3},
                              "status": {"readyReplicas": state["ready"]}}]}
        return {"items": []}

    source.read = read
    result = source.snapshot()
    assert len(result) == 1
    assert "namespace reachy-demo, deployment hello" in source.status_summary
    assert "2 of 3 replicas Ready" in source.status_summary
    state["ready"] = 3
    assert source.snapshot() == []
    assert source.status_signature == (1, 1, 3, 3, 0, 0, 0, 0)


def test_monitor_config_loads_editable_target(tmp_path: Path):
    path = tmp_path / "telemetry.yaml"
    path.write_text("source: kubernetes\nkubeconfig: /tmp/test-kubeconfig\n"
                    "namespace: app\ndeployment: api\ninterval_seconds: 15\n")
    config = load_monitor_config(path)
    assert config.namespace == "app"
    assert config.deployment == "api"
    assert config.interval_seconds == 15


def test_watchdog_alerts_only_on_changes_and_retries_same_id(monkeypatch):
    async def inline(call):
        return call()

    monkeypatch.setattr(asyncio, "to_thread", inline)
    class Source:
        state = [Finding("pod/app/api", "Pod app/api Failed")]

        def snapshot(self):
            return self.state

    class Reporter:
        calls = []

        async def deliver(self, severity, summary, event_id):
            self.calls.append((severity, event_id))
            return len(self.calls) != 1

    source, reporter = Source(), Reporter()
    monitor = Watchdog(source, reporter)

    async def scenario():
        await monitor.tick()
        await monitor.tick()
        await monitor.tick()
        source.state = []
        await monitor.tick()

    asyncio.run(scenario())
    assert [call[0] for call in reporter.calls] == ["critical", "critical", "recovering"]
    assert reporter.calls[0][1] == reporter.calls[1][1]
    assert reporter.calls[2][1] != reporter.calls[1][1]


def test_watchdog_speaks_each_check_with_new_event_id(monkeypatch):
    async def inline(call):
        return call()

    monkeypatch.setattr(asyncio, "to_thread", inline)
    class Source:
        def snapshot(self):
            return []

    class Reporter:
        def __init__(self):
            self.calls = []

        async def deliver(self, severity, summary, event_id):
            self.calls.append((severity, summary, event_id))
            return True

    reporter = Reporter()
    monitor = Watchdog(Source(), reporter, label="Kubernetes", speak_every_check=True)

    async def scenario():
        await monitor.tick()
        await monitor.tick()

    asyncio.run(scenario())
    assert len(reporter.calls) == 2
    assert reporter.calls[0][2] != reporter.calls[1][2]
    assert all("healthy" in call[1] for call in reporter.calls)


def test_watchdog_announces_initial_status_then_changes_only(monkeypatch):
    async def inline(call):
        return call()

    monkeypatch.setattr(asyncio, "to_thread", inline)

    class Source:
        state = []

        def snapshot(self):
            return self.state

    class Reporter:
        def __init__(self):
            self.calls = []

        async def deliver(self, severity, summary, event_id):
            self.calls.append((severity, summary, event_id))
            return True

    source, reporter = Source(), Reporter()
    monitor = Watchdog(source, reporter, label="Kubernetes", announce_initial=True)

    async def scenario():
        await monitor.tick()
        await monitor.tick()
        source.state = [Finding("node/reachy-k3s/not-ready", "Node reachy-k3s is not Ready")]
        await monitor.tick()
        await monitor.tick()
        source.state = []
        await monitor.tick()

    asyncio.run(scenario())
    assert [call[0] for call in reporter.calls] == ["healthy", "critical", "recovering"]


def test_docker_source_detects_restart_health_and_redacts_log_text():
    source = DockerSource()
    state = {"restart": 0, "health": "healthy", "logs": ""}

    def run(*args):
        if args[0] == "ps":
            return "abc\n"
        if args[0] == "inspect":
            return json.dumps([{"Id": "abc", "Name": "/api", "RestartCount": state["restart"],
                                "State": {"Status": "running", "Health": {"Status": state["health"]}}}])
        return state["logs"]

    source._run = run
    assert source.snapshot() == []
    state.update(restart=1, health="unhealthy", logs="ERROR secret-value\n")
    result = source.snapshot()
    assert len(result) == 3
    assert any("restarted" in item.detail for item in result)
    assert any("unhealthy" in item.detail for item in result)
    assert all("secret-value" not in item.detail for item in result)
